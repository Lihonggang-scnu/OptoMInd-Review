"""Offline syntax-recovery integration: immutable evidence and strict promotion."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from test_guide_maker import book, guide, config, offline, Factory, response, execute  # noqa: F401
from test_fullbody_cli import case, dump  # noqa: F401
from test_guide_maker_cli import guide_response
from scripts.upgrade3 import recover_guide_response as cli
from scripts.upgrade3 import guide_maker as live_cli
from optomind_research.runtime.upgrade3 import guide_maker as engine
from optomind_research.runtime.upgrade3.module4 import runtime


def malformed(value):
    value = deepcopy(value)
    value['guide']['manuscript_guide'] = 'Compare "controlled" findings and their boundaries.'
    return json.dumps(value).replace(r'\"controlled\"', '"controlled"')


class RawFactory(Factory):
    def __call__(self, role, directory, profile):
        def call(messages, **kwargs):
            self.calls.append(json.loads(messages[-1]['content']))
            return deepcopy(self.responses[len(self.calls) - 1])
        return call


def envelope(content, **extra):
    return {'content': content, 'complete': True, 'finish_reason': 'stop', **extra}


def test_automatic_repair_reads_and_cache_no_extra_calls(tmp_path, book, guide, config):
    first = envelope(malformed(response(guide, ['P0001'])))
    factory = RawFactory(first, envelope(malformed(response(guide))))
    result = execute(tmp_path, book, config, factory)
    assert result['complete'] and result['model_calls'] == 2, result
    assert len(result['read_history']) == 1
    assert 'INTACT_EVIDENCE_1' in json.dumps(factory.calls[1])
    attempt = Path(result['stages'][0]['attempt_dir'])
    raw_bytes = (attempt / 'RAW_RESPONSE.json').read_bytes()
    assert json.loads(raw_bytes) == first
    audit = json.loads((attempt / 'FORMAT_RECOVERY.json').read_text())
    assert audit['edits'] and audit['scientific_review_performed'] is False
    assert json.loads((attempt / 'NORMALIZED_RESPONSE.json').read_text())['complete'] is False
    again = execute(tmp_path, book, config, factory)
    assert again['complete'] and again['model_calls'] == 0 and len(factory.calls) == 2
    assert (attempt / 'RAW_RESPONSE.json').read_bytes() == raw_bytes


@pytest.mark.parametrize('extra', [{'finish_reason': 'length'}, {'complete': False}, {'error': 'provider_failed'}])
def test_runtime_transport_cannot_be_repaired_complete(tmp_path, book, guide, config, extra):
    factory = RawFactory(envelope(malformed(response(guide)), **extra))
    result = execute(tmp_path, book, config, factory)
    assert not result['complete'] and not (tmp_path / 'out/GUIDE.json').exists()


def test_recovery_module_invalidates_both_source_hashes():
    assert any(path.endswith('/json_format_recovery.py') for path in engine._source_file_hashes())
    assert any(path.endswith('/json_format_recovery.py') for path in live_cli._implementation_hashes())
    assert any(path.endswith('/chapter_arrangement.py') for path in engine._source_file_hashes())
    assert any(path.endswith('/chapter_arrangement.py') for path in live_cli._implementation_hashes())


def offline_args(case, tmp_path, raw, name='derived'):
    source = dump(tmp_path / 'saved/RAW_RESPONSE.json', raw)
    return ['--manifest', str(case['manifest']), '--response', str(source), '--output', str(tmp_path / name)]


def test_offline_success_no_client_ledger_or_overwrite(case, tmp_path, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail('offline recovery must never initialize paid resources')
    monkeypatch.setattr(runtime, 'QwenDirectClient', forbidden)
    monkeypatch.setattr(runtime, 'GlobalBudgetLedger', forbidden)
    monkeypatch.setattr(live_cli, 'make_live_factory', forbidden)
    args = offline_args(case, tmp_path, envelope(malformed(guide_response())))
    raw_path = tmp_path / 'saved/RAW_RESPONSE.json'
    before = raw_path.read_bytes()
    assert cli.main(args) == 0
    assert (tmp_path / 'derived/GUIDE.json').is_file()
    result = json.loads((tmp_path / 'derived/RECOVERY_RESULT.json').read_text())
    assert result['format_recovered'] and result['client_invocations'] == 0
    assert result['semantic_quality_unreviewed'] and result['original_run_modified'] is False
    assert raw_path.read_bytes() == before
    snapshot = (tmp_path / 'derived/RECOVERY_RESULT.json').read_bytes()
    assert cli.main(args) == 2
    assert (tmp_path / 'derived/RECOVERY_RESULT.json').read_bytes() == snapshot


@pytest.mark.parametrize('failure', ['length', 'false', 'reads', 'missing_chapter', 'schema', 'error_file', 'nested_error'])
def test_offline_never_promotes_incomplete_or_invalid(case, tmp_path, failure):
    value = guide_response()
    if failure == 'false':
        value['complete'] = False
    if failure == 'reads':
        value.update(complete=False, reading_needs=[{'need_id': 'N1', 'question': 'Check conditions', 'source_handles': ['P0001']}])
    if failure == 'missing_chapter':
        value['guide']['chapters'].pop()
    if failure == 'schema':
        value['extra'] = True
    raw = envelope(malformed(value))
    if failure == 'length':
        raw['finish_reason'] = 'length'
    if failure == 'nested_error':
        raw = {'choices': [{'finish_reason': 'error', 'message': {'content': raw['content']}}]}
    args = offline_args(case, tmp_path, raw)
    if failure == 'error_file':
        dump(tmp_path / 'saved/ERROR.json', {'type': 'RuntimeError', 'message': 'provider failed'})
    before = (tmp_path / 'saved/RAW_RESPONSE.json').read_bytes()
    assert cli.main(args) == 3
    assert not (tmp_path / 'derived/GUIDE.json').exists()
    result = json.loads((tmp_path / 'derived/RECOVERY_RESULT.json').read_text())
    assert result['complete'] is False
    assert (tmp_path / 'saved/RAW_RESPONSE.json').read_bytes() == before
    if failure != 'missing_chapter':
        assert (tmp_path / 'derived/DRAFT_GUIDE.json').is_file()


def test_offline_rejects_existing_original_run(case, tmp_path):
    args = offline_args(case, tmp_path, envelope(malformed(guide_response())))
    dump(tmp_path / 'saved/RUN_MANIFEST.json', {'status': 'failed'})
    args[-1] = str(tmp_path / 'saved/derived')
    assert cli.main(args) == 2
    assert not (tmp_path / 'saved/derived').exists()


def test_actual_archived_eight_quote_response_matches_oracle(tmp_path):
    import hashlib
    from optomind_research.runtime.upgrade3.guide_maker_contracts import decode_maker_response
    root = Path(__file__).resolve().parents[2]
    archive = root / 'docs/acceptance/guide-maker-max-retest-20261009'
    index = json.loads((archive / 'run/responses/maker_002/RAW_RESPONSE.json.parts.json').read_text())
    chunks = []
    for part in index['parts']:
        chunk = (archive / part['path']).read_bytes()
        assert hashlib.sha256(chunk).hexdigest() == part['sha256']
        chunks.append(chunk)
    raw_bytes = b''.join(chunks)
    assert hashlib.sha256(raw_bytes).hexdigest() == index['source_sha256']
    raw = json.loads(raw_bytes)
    before = deepcopy(raw)
    value, transport, audit = decode_maker_response(raw)
    expected = json.loads((archive / 'recovery/MODEL_RESPONSE_ESCAPING_ONLY.json').read_text())
    assert value == expected and transport is True
    assert len(audit['edits']) == 8
    assert raw == before
    assert json.loads(audit['normalized_text']) == expected


def test_runtime_invalid_schema_still_archives_candidate(tmp_path, book, guide, config):
    value = response(guide)
    value['guide']['chapters'].pop()
    result = execute(tmp_path, book, config, RawFactory(envelope(malformed(value))))
    assert not result['complete']
    attempt = Path(result['stages'][0]['attempt_dir'])
    assert (attempt / 'FORMAT_RECOVERY.json').exists()
    assert (attempt / 'NORMALIZED_RESPONSE.json').exists()
    assert not (tmp_path / 'out/GUIDE.json').exists()
