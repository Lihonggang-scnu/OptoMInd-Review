"""Opt-in companion: one Plus review, one Max revision, immutable baseline.

All checks are structural, transport and traceability checks, not a claim that
scientific quality improved. No retry, extra reading call, or BODY launch.
"""
from copy import deepcopy
import hashlib
import math
import re
from pathlib import Path
import time
from collections.abc import Mapping
import uuid

from .fullbody_writer import _cost_summary, _dependency, _execute_stage, _messages, _stage
from .writer_candidates import ROOT, _hash, _output_lock, _profile, _read, _write
from .writer_candidates_contracts import CandidateError
from .guided_body_contracts import validate_guide, _canonical_source, _chapter_mentioned_sources
from .guide_maker import _source_file_hashes, persist_format_recovery, render_guide
from .guide_maker_contracts import compile_guide_input, resolve_material_requests
from .guide_review_contracts import parse_review, parse_revision, MAX_REVIEW_BYTES

SCHEMA = 'optomind.guide_review.v1'
PROMPT_ROOT = ROOT / 'prompts/guide_review'


def validate_config(config):
    if not isinstance(config, Mapping) or set(config) - {'schema_version', 'purpose', 'reviewer', 'reviser', 'max_input_tokens'}:
        raise CandidateError('guide_review_config_invalid_keys')
    if config.get('schema_version', 'optomind.guide_review_config.v1') != 'optomind.guide_review_config.v1':
        raise CandidateError('guide_review_config_schema_invalid')
    result = deepcopy(dict(config))
    for role, model in [('reviewer', 'qwen3.5-plus'), ('reviser', 'qwen3.8-max')]:
        if not isinstance(config.get(role), Mapping):
            raise CandidateError('review_profile_required:' + role)
        result[role] = _profile(config[role], role)
        if result[role]['model'] != model:
            raise CandidateError('review_role_model_fixed:' + role)
    value = config.get('max_input_tokens')
    if value is not None and (type(value) is not int or value <= 0):
        raise CandidateError('max_input_tokens_must_be_positive_integer')
    return result


def implementation_hashes():
    hashes = _source_file_hashes()
    paths = [Path(__file__), Path(__file__).with_name('guide_review_contracts.py')]
    paths += sorted(PROMPT_ROOT.glob('*.md'))
    for path in paths:
        hashes[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _base_payload(bundle, guide, materials, omitted):
    return dict(original_guide=deepcopy(guide), full_outline=deepcopy(bundle['full_outline']),
        source_catalog=deepcopy(bundle['source_catalog']), tool_catalog=deepcopy(bundle['tool_catalog']),
        materials=deepcopy(materials), material_scope={
            'included_source_handles': [p['source_handle'] for p in materials],
            'omitted_source_handles': list(omitted), 'selection': 'only guide-explicit or guide-mentioned sources/tools and their dependencies; intact packets only',
            'complete_scientific_review': False, 'extra_model_reading_calls_allowed': False})


def _pack_materials(bundle, guide, config, counter, prompt):
    # Reuse immutable source packets from the same book, never an old draft or
    # prior maker's hidden session. No network reads or model selectors.
    pack = bundle['science_archive']
    handles = list(dict.fromkeys(_canonical_source(pack, h)
        for c in guide['chapters'] for h in [*c.get('source_handles', []), *_chapter_mentioned_sources(pack, c)]))
    # Tool packets require an explicit address in guide prose; catalog presence
    # alone never justifies filling the model context with unrelated science.
    import json
    prose = json.dumps(guide, ensure_ascii=False)
    tools = [r['tool_handle'] for r in bundle['tool_catalog']
             if re.search(r'(?<![A-Za-z0-9_])' + re.escape(r['tool_handle']) + r'(?![A-Za-z0-9_])', prose)]
    needs = [dict(need_id='N1', question='Inspect original guide against linked intact evidence.',
                  source_handles=handles, tool_handles=tools)] if handles or tools else []
    packets = resolve_material_requests(bundle, needs)
    chosen = []
    omitted = list(dict.fromkeys([r['source_handle'] for r in bundle['source_catalog']] +
                                 [r['tool_handle'] for r in bundle['tool_catalog']]))
    for packet in packets:
        remaining = [h for h in omitted if h != packet['source_handle']]
        candidate = _base_payload(bundle, guide, chosen + [packet], remaining)
        reviewer = _stage('capacity', 'reviewer', _messages(prompt['reviewer'], candidate), config['reviewer'], config, counter)
        if reviewer['estimate']['fits'] and _revision_capacity(candidate, config, counter, prompt['reviser']):
            chosen.append(packet)
            omitted = remaining
    return _base_payload(bundle, guide, chosen, omitted)


def _revision_capacity(payload, config, counter, prompt):
    stage = _stage('revision_preflight', 'reviser', _messages(prompt, payload), config['reviser'], config, counter)
    # Feedback is bounded in UTF-8 bytes by the parser. One token per byte is
    # a conservative tokenizer-independent allowance. Wire JSON escapes the
    # inner JSON again, so reserve doubled bytes plus nesting/framing.
    allowance = _feedback_token_allowance(config)
    return stage['estimate']['reserved_input_tokens'] + allowance <= stage['estimate']['input_limit_tokens']


def _feedback_token_allowance(config):
    return math.ceil((2 * MAX_REVIEW_BYTES + 2048) * config['reviser']['prompt_token_multiplier'])


def run_guide_review(book, guide, output_dir, config, client_factory=None, run=False, token_counter=None):
    output = Path(output_dir).resolve()
    with _output_lock(output):
        return _run(book, guide, output, config, client_factory, run, token_counter)


def _run(book, guide, output, config, factory, run, counter):
    config = validate_config(config)
    guide = validate_guide(guide, book)
    bundle = compile_guide_input(book)
    hashes = implementation_hashes()
    prompts = {r: (PROMPT_ROOT / f'{name}.md').read_text(encoding='utf-8')
               for r, name in [('reviewer', 'review'), ('reviser', 'revise')]}
    mode = getattr(factory, 'execution_mode', 'injected' if factory else 'preview')
    identity = dict(schema_version=SCHEMA, book_sha256=_hash(book), baseline_sha256=_hash(guide),
                    config=config, implementation_hashes=hashes)
    binding = output / 'EXPERIMENT_INPUT.json'
    if binding.exists() and _read(binding) != identity:
        raise CandidateError('review_input_or_config_changed_use_new_experiment_output')
    # Identity is frozen even for preview; caller must keep the same inputs.
    _write(binding, identity)
    baseline_path = output / 'BASELINE_GUIDE.json'
    if baseline_path.exists() and _read(baseline_path) != guide:
        raise CandidateError('review_baseline_changed')
    if not baseline_path.exists():
        _write(baseline_path, guide)
        _write(output / 'BASELINE_GUIDE.md', render_guide(guide), text=True)
    execution_path = output / 'EXECUTION_BINDING.json'
    execution = {'mode': mode, 'fixture_sha256': getattr(factory, 'fixture_sha256', None)}
    if run:
        if execution_path.exists() and _read(execution_path) != execution:
            raise CandidateError('review_execution_mode_or_fixture_changed')
        _write(execution_path, execution)
    run_id = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:10]
    run_dir = output / 'runs' / run_id
    run_dir.mkdir(parents=True)
    _write(output / 'inputs' / identity['book_sha256'] / 'FULL_BODY_INPUT.json', book)
    payload = _pack_materials(bundle, guide, config, counter, prompts)
    payload_binding = output / 'PAYLOAD_BINDING.json'
    frozen_payload = {'payload_sha256': _hash(payload)}
    if payload_binding.exists() and _read(payload_binding) != frozen_payload:
        raise CandidateError('review_material_selection_changed_use_new_experiment_output')
    _write(payload_binding, frozen_payload)
    _write(run_dir / 'REVIEW_INPUT.json', payload)
    stages, dependencies = [], []
    review, revision, status = None, None, 'preview'
    for role in ('reviewer', 'reviser'):
        stage_payload = deepcopy(payload)
        if role == 'reviser':
            stage_payload['advisory_review'] = {k: deepcopy(review[k]) for k in ('findings', 'limitations')}
        stage = _stage(role + '_001', role, _messages(prompts[role], stage_payload), config[role], config, counter)
        if role == 'reviewer' and not _revision_capacity(payload, config, counter, prompts['reviser']):
            stage.update(status='capacity_blocked', required_action='Baseline plus bounded review feedback exceeds Max capacity; no review dispatched. Use a verified tokenizer or larger supported capacity, never truncate inputs.')
        request_binding = output / (role.upper() + '_REQUEST_BINDING.json')
        frozen_request = {'messages_sha256': _hash(stage['messages']),
                          'wire_request_sha256': stage['estimate']['wire_request_sha256']}
        if request_binding.exists() and _read(request_binding) != frozen_request:
            raise CandidateError('review_request_changed_use_new_experiment_output:' + role)
        _write(request_binding, frozen_request)
        parser = (lambda response: parse_review(response, payload)) if role == 'reviewer' else (
            lambda response: parse_revision(response, book, bundle, review, guide))
        outcome = _execute_stage(stage, output=output, run_dir=run_dir, route='guide_review',
            code_hash=_hash(hashes), input_hash=_hash(identity), dependencies=deepcopy(dependencies),
            client_factory=factory, run=run, retry_failed=False, config=config, counter=counter, parser=parser)
        parsed = outcome.get('result', {})
        if parsed.get('provider_transport_complete') is False:
            # Keep the nested-envelope result, not the shared shallow flag.
            parsed.update(complete=False, transport_complete=False)
            outcome.update(status='pending', result_sha256=_hash(parsed))
            if outcome.get('attempt_dir'):
                _write(Path(outcome['attempt_dir']) / 'RESULT.json', parsed)
        if parsed.get('format_recovery') and outcome.get('attempt_dir'):
            attempt = Path(outcome['attempt_dir'])
            outcome['format_recovery_path'] = persist_format_recovery(attempt, parsed, attempt / 'RAW_RESPONSE.json')
        stages.append(outcome)
        _write(run_dir / 'STAGES.json', stages)
        if not parsed.get('complete'):
            status = 'preview' if outcome['status'] == 'planned' else outcome['status'] if 'result' not in outcome else 'response_invalid'
            break
        dependencies.append(_dependency(outcome))
        if role == 'reviewer':
            review = parsed
            _write(output / 'REVIEW.json', review)
        else:
            revision = parsed
            status = 'complete'
    complete = status == 'complete'
    cost = _cost_summary(stages, None)
    if mode in ('recording', 'replay', 'preview'):
        cost.update(candidate_stage_known_cost_cny=0, total_known_cost_including_base_cny=0,
                    candidate_stage_cost_complete=True, total_cost_complete=True, unknown_cost_attempts=[],
                    accounting_note='Offline execution has no provider charges; original baseline cost excluded.')
        for row in cost['all_attempts_including_retries']:
            row['estimated_actual_cost_cny'] = 0
    result = dict(schema_version=SCHEMA + '.result', run_id=run_id, status=status, complete=complete,
        execution_mode=mode, baseline_sha256=identity['baseline_sha256'], baseline_path=str(baseline_path),
        revised_path=str(output / 'REVISED_GUIDE.json') if complete else None,
        selected_guide_path=str(baseline_path), selection_pending=complete,
        candidate_guide_path=str(output / 'REVISED_GUIDE.json') if complete else None,
        guide=revision['guide'] if complete else guide, review=review,
        decisions=revision['decisions'] if complete else [], material_scope=payload['material_scope'],
        model_calls=sum(s.get('model_calls', 0) for s in stages),
        paid_dispatch_count=None if any(s.get('paid_dispatch_count') is None for s in stages) else sum(s.get('paid_dispatch_count', 0) for s in stages),
        stages=stages, cost_summary=cost, max_calls_per_frozen_experiment=2,
        automatic_paid_retries=False, baseline_preserved=True, scientific_correctness_established=False,
        feedback_max_utf8_bytes=MAX_REVIEW_BYTES, feedback_reserved_input_tokens=_feedback_token_allowance(config),
        semantic_quality_unreviewed=True, writing_tested=False)
    if complete:
        _write(output / 'REVISED_GUIDE.json', revision['guide'])
        _write(output / 'REVISED_GUIDE.md', render_guide(revision['guide']), text=True)
        _write(output / 'DECISIONS.json', revision['decisions'])
    _write(run_dir / 'REVIEW_RESULT.json', result)
    previous_path = output / 'REVIEW_RESULT.json'
    previous = _read(previous_path) if previous_path.exists() else None
    if previous and previous.get('complete') and not complete:
        result.update(previous_revision_preserved=True,
                      selected_guide_path=previous['selected_guide_path'],
                      candidate_guide_path=previous['candidate_guide_path'], selection_pending=True,
                      selected_complete_run_id=previous['run_id'])
        _write(run_dir / 'REVIEW_RESULT.json', result)
    else:
        _write(previous_path, result)
    return result
