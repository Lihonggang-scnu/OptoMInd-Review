import json
from argparse import Namespace
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger, QwenTransportError
from optomind_research.runtime.upgrade3.serial_parts_live import run_live_parts
from scripts.upgrade3.writer_candidates import make_live_factory


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
