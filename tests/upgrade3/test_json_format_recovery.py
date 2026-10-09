"""Offline regression oracle and conservative syntax-only recovery boundaries."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import json_format_recovery as recovery
from optomind_research.runtime.upgrade3.guide_maker_contracts import decode_maker_response
from optomind_research.runtime.upgrade3.writer_candidates_contracts import CandidateError


def _assert_audit(original, expected):
    value, audit = recovery.recover_json_format(original)
    assert value == expected
    assert audit['original_sha256'] == hashlib.sha256(original.encode()).hexdigest()
    assert audit['normalized_sha256'] == hashlib.sha256(audit['normalized_text'].encode()).hexdigest()
    replay = original
    for edit in reversed(audit['edits']):
        i = edit['original_offset']
        assert replay[i:i + len(edit['original'])] == edit['original']
        replay = replay[:i] + edit['replacement'] + replay[i + len(edit['original']):]
    assert replay == audit['normalized_text']
    assert recovery.strict_json_loads(replay) == expected
    return audit


def test_actual_archived_raw_matches_escaping_only_oracle():
    archive = Path(__file__).resolve().parents[2] / 'docs/acceptance/guide-maker-max-retest-20261009'
    index = json.loads((archive / 'run/responses/maker_002/RAW_RESPONSE.json.parts.json').read_text())
    data = b''.join((archive / part['path']).read_bytes() for part in index['parts'])
    assert hashlib.sha256(data).hexdigest() == index['source_sha256']
    original = json.loads(data)['content']
    expected = json.loads((archive / 'recovery/MODEL_RESPONSE_ESCAPING_ONLY.json').read_text())
    audit = _assert_audit(original, expected)
    assert len(audit['edits']) == 8
    assert all(e['operation'] == 'escape_inner_quote' for e in audit['edits'])
    assert len(audit['normalized_text']) == len(original) + 8


def test_strict_valid_input_never_calls_repair(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError('strict valid input must not invoke repair')
    monkeypatch.setattr(recovery, 'repair_json', fail)
    original = json.dumps({'science': 'P0033\\n 1e-7 "quoted", ] } :', 'value': 1.2e-9})
    assert recovery.recover_json_format(original) == (json.loads(original), None)


@pytest.mark.parametrize('raw, expected', [
    ('{"x":"原文按"引用"推进", "source":"P0033", "n":1.25e-7}',
     {'x':'原文按"引用"推进', 'source':'P0033', 'n':1.25e-7}),
    ('{"x":"say "hello" world"}', {'x':'say "hello" world'}),
    ('\ufeff```json\n{"x": [1,2,],}\n```\n', {'x':[1,2]}),
    ('{"x":"line\nnext\tend\x00"}', {'x':'line\nnext\tend\x00'}),
    ('{"x":"comma, ] text", "y": [1,],}', {'x':'comma, ] text', 'y':[1]}),
    ('{"x":"backslash\\\\ retained and "quotes" here",}',
     {'x':'backslash\\ retained and "quotes" here'}),
])
def test_supported_format_only_recovery(raw, expected):
    _assert_audit(raw, expected)


@pytest.mark.parametrize('raw', [
    'prefix {"x":1}', '{"x":1} suffix', '```json\n{"x":1}',
    '{"x":1', '{"x":"unfinished}', '{"x":"unfinished', '{"x":',
    '{"x":1,"x":2}', '{"x":1,"x":1,}', '{"x":"a", "x":"b"}',
    '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', '{"x":1e999}',
    '{"x":1e999,}', '{"x":undefined}', "{'x':1}", '{x:1}',
    '["a" "b"]', '{"a":1 "b":2}', '{"x":"a"b"}',
    '{"x":"raw\\q"}', '[1,,2]', '{"x":}', '{"x":true false}',
    '{"x":"a"b":1}',
])
def test_refuses_ambiguous_incomplete_semantic_or_conflicting_input(raw):
    with pytest.raises(ValueError):
        recovery.recover_json_format(raw)


def test_repair_library_must_agree(monkeypatch):
    monkeypatch.setattr(recovery, 'repair_json', lambda *a, **k: {'x':'invented'})
    with pytest.raises(recovery.JsonFormatError, match='disagreement'):
        recovery.recover_json_format('{"x":"a"b"c"}')


def test_size_and_edit_bounds(monkeypatch):
    monkeypatch.setattr(recovery, 'MAX_RECOVERY_CHARS', 10)
    with pytest.raises(recovery.JsonFormatError, match='size_limit'):
        recovery.recover_json_format('{"x": [1,2,3,]}')
    monkeypatch.setattr(recovery, 'MAX_RECOVERY_CHARS', 1000)
    monkeypatch.setattr(recovery, 'MAX_RECOVERY_EDITS', 1)
    with pytest.raises(recovery.JsonFormatError, match='edit_limit'):
        recovery.recover_json_format('{"x":"say "hello" now"}')


@pytest.mark.parametrize('reason', ['length','max_tokens','max_output_tokens','error','content_filter','cancelled','timeout'])
def test_decode_preserves_transport_failure_and_raw(reason):
    raw = {'choices': [{'finish_reason':reason, 'message':{'content':'{"x":"a"b"c"}'}}]}
    before = deepcopy(raw)
    value, transport, audit = decode_maker_response(raw)
    assert value == {'x':'a"b"c'} and not transport and audit
    assert raw == before


def test_decode_rejects_invalid_json_as_contract_error():
    with pytest.raises(CandidateError, match='maker_response_invalid_json'):
        decode_maker_response({'content':'{"complete":true'})
