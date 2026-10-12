import json
import hashlib
from argparse import Namespace
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger, QwenTransportError
from optomind_research.runtime.upgrade3.serial_parts_live import run_live_parts
from scripts.upgrade3.writer_candidates import make_live_factory
from optomind_research.runtime.upgrade3 import serial_parts_live as live
from optomind_research.runtime.upgrade3.serial_manuscript_parts import SerialPartsError, parse_stage_response


def inputs(tmp_path):
    draft = tmp_path / "body.md"
    draft.write_text("# 原题\n\n## 实质正文\n\n条件限定关系 [P0001]。\n", encoding="utf8")
    project, account = tmp_path / "project.sqlite", tmp_path / "account.sqlite"
    GlobalBudgetLedger(path=project, limit_cny=60)
    GlobalBudgetLedger(path=account, limit_cny=300)
    args = Namespace(output=tmp_path / "out", draft=draft, body_report=None,
        identity_catalog=[], thinking_budget=16384, output_tokens=32768,
        incremental_budget=5, ledger=project, budget_limit=60,
        account_ledger=account, account_budget_limit=300,
        account_mapping_dir=tmp_path / "mappings", research_question="什么限定关系？",
        language="zh", run=True)
    return args


def responses():
    part = {"purpose": "读者任务", "focus": ["条件与关系"], "boundary": ["实际正文范围"],
        "placement": {"mode": "standalone", "anchor": "manuscript_start"},
        "finalize_from": ["实际正文"]}
    return {"conception": {"manuscript_parts_plan": {"context": "普通综述读者",
        **{key: part for key in ("abstract", "introduction", "conclusion")}}},
        "conclusion": {"conclusion": "关系受到条件限定 [P0001]。"},
        "introduction": {"introduction": "本篇解释条件与关系 [P0001]。"},
        "abstract": {"title": "条件与关系", "abstract": "本文解释有条件关系。", "keywords": ["条件"]}}


def test_live_adapter_four_exact_stages_then_free_cache_reassembly(tmp_path):
    args = inputs(tmp_path)
    calls = []
    def factory(role, attempt, profile):
        def client(messages, **kwargs):
            calls.append(role)
            return {"content": json.dumps(responses()[role], ensure_ascii=False),
                "complete": True, "finish_reason": "stop", "returned_model": "qwen3.5-plus",
                "transport_events": "audit" * 45000}
        return client
    context = {"source_identity_map": {"P0001": {"title": "来源", "doi": "10.1/a"}}}
    original = args.draft.read_bytes()
    first = run_live_parts(args=args, context=context, chapter_roles=[],
        token_counter=lambda raw, messages: 100, tokenizer_info={"mode": "test"}, client_factory=factory)
    assert first["status"] == "generated"
    assert first["model_calls"] == 4
    assert "transport_events" in json.loads((args.output / "cache/conception/RAW_RESPONSE.json").read_text())
    assert "transport_events" not in json.loads((Path(first["attempt_dir"]) / "responses/conception.json").read_text())
    assert calls == ["conception", "conclusion", "introduction", "abstract"]
    second = run_live_parts(args=args, context=context, chapter_roles=[],
        token_counter=lambda raw, messages: 100, tokenizer_info={"mode": "test"}, client_factory=factory)
    assert second["status"] == "generated"
    assert second["model_calls"] == 0 and len(calls) == 4
    assert first["budget_binding"] == second["budget_binding"]
    assert Path(first["final_manuscript"]).read_bytes() == Path(second["final_manuscript"]).read_bytes()
    assert args.draft.read_bytes() == original
    args.incremental_budget = 6
    with pytest.raises(ValueError, match="binding_changed"):
        run_live_parts(args=args, context=context, chapter_roles=[], token_counter=None,
            tokenizer_info={"mode": "test"}, client_factory=factory)


def test_factory_incremental_ceiling_is_atomic_and_does_not_change_stored_cap(tmp_path, monkeypatch):
    import optomind_research.runtime.upgrade3.module4.runtime as runtime
    args = inputs(tmp_path)
    args.responses = None
    args.allow_max = False
    args.budget_ledger = args.ledger
    args.key_file = tmp_path / "absent-private-key"
    args.key_index = 1
    args.account_ledger = None
    captured = []
    class Client:
        def __init__(self, **kwargs):
            captured.append(kwargs["budget_ledger"])
            self.__dict__.update(kwargs)
            self.prompt_token_multiplier = 1.1
            self.prompt_token_framing_margin = 512
        def __call__(self, messages, **kwargs):
            return {}
    monkeypatch.setattr(runtime, "QwenDirectClient", Client)
    from optomind_research.runtime.upgrade3.writer_candidates import _profile
    factory = make_live_factory(args, reservation_ceiling_cny=5)
    factory("conception", tmp_path / "attempt", _profile({}, "test"))
    ledger = captured[0]
    ledger.reserve(4.9, "first")
    with pytest.raises(QwenTransportError, match="global_budget_exceeded"):
        ledger.reserve(0.2, "refused")
    assert GlobalBudgetLedger(path=args.ledger).as_dict()["limit_cny"] == 60
    assert not args.key_file.exists()


def test_explicit_format_repair_preserves_invalid_raw_and_resumes_for_free(tmp_path):
    args = inputs(tmp_path)
    calls = []
    def factory(role, attempt, profile):
        def client(messages, **kwargs):
            calls.append(role)
            data = responses()[role.replace("_format_repair", "")]
            if role == "conception":
                data["manuscript_parts_plan"]["abstract"]["boundary"] = "实际正文范围"
            return {"content": json.dumps(data, ensure_ascii=False), "complete": True,
                "finish_reason": "stop", "returned_model": "qwen3.5-plus"}
        return client
    opts = dict(args=args, context={}, chapter_roles=[],
        token_counter=lambda raw, messages: 100, tokenizer_info={"mode": "test"}, client_factory=factory)
    failed = run_live_parts(**opts)
    assert failed["status"] == "failed" and failed["failed_stage"] == "conception"
    assert "nonempty_array_required" in failed["error"] and calls == ["conception"]
    args.repair_invalid = True
    repaired = run_live_parts(**opts)
    assert repaired["status"] == "generated" and repaired["model_calls"] == 4
    original = json.loads((args.output / "cache/conception/RAW_RESPONSE.json").read_text())
    assert isinstance(json.loads(original["content"])["manuscript_parts_plan"]["abstract"]["boundary"], str)
    assert (args.output / "cache/conception_format_repair/PARSED_RESPONSE.json").is_file()
    replayed = run_live_parts(**opts)
    assert replayed["status"] == "generated" and replayed["model_calls"] == 0
    assert len(calls) == 5 and repaired["budget_binding"] == failed["budget_binding"]


@pytest.mark.parametrize("problem", ["unknown_handle", "unsupported_placement"])
def test_scientific_identity_and_placement_failures_never_trigger_format_repair(tmp_path, problem):
    args = inputs(tmp_path)
    args.repair_invalid = True
    calls = []
    def factory(role, attempt, profile):
        def client(messages, **kwargs):
            calls.append(role)
            data = responses()[role]
            part = data["manuscript_parts_plan"]["abstract"]
            if problem == "unknown_handle":
                part["focus"] = ["无法归属的引用 [P9999]"]
            else:
                part["placement"]["mode"] = "embedded"
            return {"content": json.dumps(data, ensure_ascii=False), "complete": True,
                "finish_reason": "stop", "returned_model": "qwen3.5-plus"}
        return client
    result = run_live_parts(args=args, context={}, chapter_roles=[],
        token_counter=lambda raw, messages: 100, tokenizer_info={}, client_factory=factory)
    assert result["status"] in {"failed", "unsupported_placement"}
    assert calls == ["conception"]
    assert not (args.output / "cache/conception_format_repair").exists()


def test_preview_never_reads_key_or_constructs_client_or_writes_ledgers(tmp_path, monkeypatch):
    args = inputs(tmp_path)
    args.run = False
    args.key_file = tmp_path / "not-a-real-credential"
    before = {p: p.read_bytes() for p in (args.ledger, args.account_ledger)}
    def forbidden(*a, **kw):
        raise AssertionError("preview must not construct a live client")
    monkeypatch.setattr("scripts.upgrade3.writer_candidates.make_live_factory", forbidden)
    result = run_live_parts(args=args, context={}, chapter_roles=[],
        token_counter=lambda raw, messages: 100, tokenizer_info={})
    assert result["status"] == "preview" and result["model_calls"] == 0
    assert not args.key_file.exists()
    assert all(p.read_bytes() == original for p, original in before.items())


def test_downstream_prompt_change_rejects_old_binding_before_any_call(tmp_path, monkeypatch):
    from optomind_research.runtime.upgrade3 import serial_manuscript_parts as serial
    args = inputs(tmp_path)
    args.run = False
    opts = dict(args=args, context={}, chapter_roles=[],
        token_counter=lambda raw, messages: 100, tokenizer_info={})
    first = run_live_parts(**opts)
    binding = (args.output / "LIVE_BINDING.json").read_bytes()
    assert first["budget_binding"]["inputs"]["prompt_contract_sha256"] == serial.prompt_contract_sha256()
    monkeypatch.setitem(serial._DUTIES, "abstract", serial._DUTIES["abstract"] + " New requirement.")
    args.run = True
    def forbidden(*a, **kw):
        pytest.fail("Changed binding must fail before constructing a client")
    with pytest.raises(ValueError, match="binding_changed"):
        run_live_parts(**opts, client_factory=forbidden)
    assert (args.output / "LIVE_BINDING.json").read_bytes() == binding
    assert not (args.output / "cache").exists()


@pytest.mark.parametrize('stage', ['introduction', 'conclusion', 'abstract'])
def test_paragraph_array_normalization_is_lossless_and_keeps_other_fields(stage):
    data = responses()[stage]
    paragraphs = ['  First paragraph [P0001].\r\nKeep this line. ', '\tSecond paragraph.  ']
    data[stage] = paragraphs
    original = {'content': json.dumps(data, ensure_ascii=False), 'finish_reason': 'stop'}
    response, receipt = live._normalize_paragraph_response(stage, original)
    parsed = parse_stage_response(stage, response, allowed_source_handles=['P0001'])
    assert parsed[stage] == '\n\n'.join(paragraphs)
    assert json.loads(original['content'])[stage] == paragraphs
    assert receipt['source_content_sha256'] == hashlib.sha256(original['content'].encode('utf-8')).hexdigest()
    assert receipt['field'] == stage and receipt['original_type'] == 'array' and receipt['normalized_type'] == 'string'
    if stage == 'abstract':
        assert parsed['title'] == data['title'] and parsed['keywords'] == data['keywords']


@pytest.mark.parametrize('paragraphs', [[], ['First.', ''], ['First.', '  '], ['First.', {}], ['First.', 1]])
def test_mixed_or_empty_paragraph_arrays_keep_core_rejection(paragraphs):
    original = {'content': json.dumps({'introduction': paragraphs}), 'finish_reason': 'stop'}
    response, receipt = live._normalize_paragraph_response('introduction', original)
    assert response == original and receipt is None
    with pytest.raises(SerialPartsError, match='nonempty_string_required:introduction'):
        parse_stage_response('introduction', response)


@pytest.mark.parametrize('content,error', [
    ('{"introduction":["First."],"extra":"Unexpected"}', 'response_wrong_keys'),
    ('{"introduction":["First [P9999]."]}', 'unsupported_source_handles'),
    ('{"introduction":["First."],"introduction":["Second."]}', 'duplicate_response_key'),
])
def test_normalization_preserves_core_key_handle_and_duplicate_validation(content, error):
    with pytest.raises(SerialPartsError, match=error):
        response, _ = live._normalize_paragraph_response('introduction', {'content': content, 'finish_reason': 'stop'})
        parse_stage_response('introduction', response, allowed_source_handles=['P0001'])


def _seed_legacy_array_repair(tmp_path, monkeypatch):
    args = inputs(tmp_path)
    args.repair_invalid = True
    calls = []
    paragraphs = ['  First paragraph [P0001]. ', 'Second paragraph.\r\nKeep this line.']
    def factory(role, attempt, profile):
        def client(messages, **kwargs):
            calls.append(role)
            content = '{"introduction":"First." "Second."}' if role == 'introduction' else json.dumps(
                {'introduction': paragraphs} if role == 'introduction_format_repair' else responses()[role], ensure_ascii=False)
            return {'content': content, 'complete': True, 'finish_reason': 'stop', 'returned_model': 'qwen3.5-plus'}
        return client
    opts = dict(args=args, context={}, chapter_roles=[], token_counter=lambda raw, messages: 100,
                tokenizer_info={'mode': 'test'}, client_factory=factory)
    original_builder = live._format_repair_messages
    with monkeypatch.context() as patch:
        patch.setattr(live, '_normalize_paragraph_response', lambda stage, response: (response, None))
        patch.setattr(live, '_format_repair_messages', lambda stage, messages, content, error, **kw:
                      original_builder(stage, messages, content, error, legacy=True))
        failed = run_live_parts(**opts)
    assert failed['status'] == 'failed' and failed['failed_stage'] == 'introduction'
    assert 'nonempty_string_required:introduction' in failed['error']
    assert calls == ['conception', 'conclusion', 'introduction', 'introduction_format_repair']
    return opts, failed, calls, paragraphs


def test_legacy_repair_cache_is_normalized_for_free_and_only_missing_abstract_calls(tmp_path, monkeypatch):
    opts, failed, calls, paragraphs = _seed_legacy_array_repair(tmp_path, monkeypatch)
    output = opts['args'].output
    protected = {path: path.read_bytes() for path in (output / 'cache').rglob('*')
                 if path.name in {'RAW_RESPONSE.json', 'MODEL_CONTENT.txt', 'MESSAGES.json', 'REQUEST.json'}}
    ledgers = {path: path.read_bytes() for path in (opts['args'].ledger, opts['args'].account_ledger)}
    resumed = run_live_parts(**opts)
    assert resumed['status'] == 'generated' and resumed['model_calls'] == 1
    assert calls[-1] == 'abstract' and len(calls) == 5
    assert resumed['budget_binding'] == failed['budget_binding']
    generated = json.loads((Path(resumed['attempt_dir']) / 'GENERATED_PARTS.json').read_text(encoding='utf-8'))
    assert generated['introduction'] == '\n\n'.join(paragraphs)
    receipt = json.loads((output / 'cache/introduction_format_repair/NORMALIZATION_RECEIPT.json').read_text(encoding='utf-8'))
    assert receipt['source_cache_stage'] == 'introduction_format_repair'
    assert receipt['source_model_content_sha256'] == hashlib.sha256(protected[output / 'cache/introduction_format_repair/MODEL_CONTENT.txt']).hexdigest()
    assert all(path.read_bytes() == before for path, before in protected.items())
    assert all(path.read_bytes() == before for path, before in ledgers.items())
    replayed = run_live_parts(**opts)
    assert replayed['status'] == 'generated' and replayed['model_calls'] == 0 and len(calls) == 5


@pytest.mark.parametrize('tamper', ['messages', 'request', 'missing_request'])
def test_legacy_repair_keeps_stage_messages_and_request_binding(tmp_path, monkeypatch, tamper):
    opts, _, calls, _ = _seed_legacy_array_repair(tmp_path, monkeypatch)
    repair = opts['args'].output / 'cache/introduction_format_repair'
    path = repair / ('MESSAGES.json' if tamper == 'messages' else 'REQUEST.json')
    if tamper == 'missing_request':
        path.unlink()
    else:
        data = json.loads(path.read_text(encoding='utf-8'))
        if tamper == 'messages':
            data[-1]['content'] += ' unrelated task'
        else:
            data['messages_sha256'] = 'wrong'
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    result = run_live_parts(**opts)
    assert result['status'] == 'failed' and result['failed_stage'] == 'introduction'
    assert result['model_calls'] == 0 and len(calls) == 4
    assert 'mismatch' in result['error'] or 'identity_changed' in result['error'] or 'request_missing' in result['error']


@pytest.mark.parametrize('stage,hint', [
    ('conception', 'focus、boundary、finalize_from为非空字符串数组'),
    ('introduction', 'introduction必须是单个非空字符串'),
    ('conclusion', 'conclusion必须是单个非空字符串'),
    ('abstract', 'title和abstract必须是单个非空字符串；keywords必须是非空字符串数组'),
])
def test_format_repair_schema_hint_matches_stage(stage, hint):
    messages = live._format_repair_messages(stage, [], 'bad', 'response_not_json:' + stage)
    assert hint in messages[-1]['content']
