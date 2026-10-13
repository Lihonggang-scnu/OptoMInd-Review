"""Round-local paid transport. No credential contents are logged or copied."""
from __future__ import annotations
import importlib.util
import json
import os
import sqlite3
import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REVIEW = Path('D:/OptoMind-Article-3/repos/OptoMInd-Review')
KEY_FILE = Path('F:/OptoMind-Review-2/api_keys/qwen-api-key.txt')
MODELS = {'qwen3.5-plus', 'qwen3.7-flash'}
SOURCE = REVIEW / 'optomind_research/runtime/upgrade3/module4/runtime.py'
spec = importlib.util.spec_from_file_location('trial_frozen_m4_transport', SOURCE)
native = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = native
spec.loader.exec_module(native)
LEDGER = native.GlobalBudgetLedger(limit_cny=15.0, path=ROOT / 'budget.sqlite')
_lock = threading.RLock()

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')

def assert_round_open():
    with sqlite3.connect(LEDGER.path) as db:
        if db.execute("SELECT COUNT(*) FROM reservations WHERE status IN ('reserved','uncertain')").fetchone()[0]:
            raise RuntimeError('Round has unresolved spend; no further model requests permitted')

class TrialClient:
    """Supports the production direct-client protocol; bound to one round ledger."""
    def __init__(self, *, model='qwen3.7-flash', lane='review', **configuration):
        if model not in MODELS:
            raise ValueError('Model outside user-authorized whitelist')
        self.model = self.model_name = model
        self.lane = lane
        self.configuration = dict(configuration)

    def __call__(self, messages, **kwargs):
        model = kwargs.pop('model', self.model)
        if model != self.model or model not in MODELS:
            raise ValueError('Per-call model mismatch')
        if kwargs.get('force_mock'):
            raise ValueError('Mock output cannot enter this real trial')
        output = int(kwargs.pop('max_output_tokens', self.configuration.get('max_output_tokens', 8192)))
        thinking = bool(kwargs.pop('thinking', self.configuration.get('thinking', False)))
        thinking_budget = int(kwargs.pop('thinking_budget', self.configuration.get('thinking_budget', 2048))) if thinking else 0
        if output < 64 or output + thinking_budget > 32768:
            raise ValueError('Explicit trial output ceiling exceeded')
        call_id = self.lane + '-' + uuid.uuid4().hex[:14]
        directory = ROOT / 'calls' / call_id
        with _lock:
            assert_round_open()
            request_record = dict(model=model, messages=list(messages), max_output_tokens=output,
                                  thinking=thinking, thinking_budget=thinking_budget,
                                  retries=0, key_slots=1, endpoint='dashscope China compatible-mode/v1',
                                  production_call_id=kwargs.get('call_id'), lane=self.lane)
            write_json(directory / 'REQUEST.json', request_record)
            client = native.QwenDirectClient(
                model=model, key_file=KEY_FILE, key_index=1,
                max_retries=0, max_keys=1, timeout_seconds=300,
                max_output_tokens=output, thinking=thinking, thinking_budget=thinking_budget,
                json_mode=not thinking or model == 'qwen3.7-flash',
                raw_response_dir=directory / 'raw', budget_ledger=LEDGER,
                prompt_token_counter=lambda body, messages: len(body) + 4096,
            )
            allowed = {key: kwargs[key] for key in ('temperature', 'stream', 'transport_observer') if key in kwargs}
            try:
                result = client(messages, call_id=call_id, **allowed)
                write_json(directory / 'RESPONSE.json', result)
                return result
            except Exception as exc:
                write_json(directory / 'ERROR.json', {'type': type(exc).__name__,
                           'code': getattr(exc, 'code', None), 'record': getattr(exc, 'record', {})})
                raise
            finally:
                write_json(ROOT / 'BUDGET_STATUS.json', LEDGER.as_dict())

    def call(self, messages, *, max_tokens=4000, force_mock=None):
        if self.lane == 'article-compile':
            messages = [dict(message) for message in messages]
            messages[0]['content'] += ('\nTrial physical contract: the two coating layers have explicitly constant, lossless indices 1.46 and 2.40. '
                'Represent each with material=null, constant_n equal to its stated index and constant_k=0.0. '
                'Do not substitute named dispersive registry materials. Use exactly two finite layers, 51 wavelength points from 450 to 700 nm, and optimizer seed 17.')
        result = self(messages, max_output_tokens=max_tokens, force_mock=force_mock)
        usage = dict(result.get('usage') or {})
        return {**result, '_llm_usage': {**usage, 'model_name': self.model,
                'mock_llm': False, 'cost_cny': native.estimated_cost_cny(usage, model=self.model)}}

def install_article_clients():
    """Replace ONLY the transport boundary; production analyzers/loop/solver stay real."""
    from optomind_optics.harness.problem_analyzer import ArticlePlusQwenClient
    from optomind_optics.harness.qwen_policy import QwenFlashOnlyClient
    def initialize(client, *, role='plus'):
        client.role = role
    def invoke(client, messages, *, max_tokens=4000, force_mock=None):
        return TrialClient(model=client.model_name, lane='article').call(
            messages, max_tokens=max_tokens, force_mock=force_mock)
    ArticlePlusQwenClient.__init__ = initialize
    ArticlePlusQwenClient.call = invoke
    QwenFlashOnlyClient.call = invoke

if __name__ == '__main__':
    write_json(ROOT / 'BUDGET_STATUS.json', LEDGER.as_dict())
    print(json.dumps({'limit_cny': 15, 'spent_cny': LEDGER.as_dict()['actual_cny'],
                      'models': sorted(MODELS), 'paid_calls_made_by_this_command': 0}))
