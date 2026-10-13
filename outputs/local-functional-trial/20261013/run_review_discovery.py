"""Fresh query planning and bounded real Semantic Scholar corpus retrieval."""
import json
import os
from trial_transport import ROOT, TrialClient, write_json
os.environ['OPTOMIND_ECONOMY_TEXT_CEILING'] = '0'
os.environ['SEMANTIC_SCHOLAR_API_KEYS_FILE'] = 'F:/OptoMind-Review-2/api_keys/semantic-scholar-api-key.txt'
os.environ['OPTOMIND_S2_CACHE_PATH'] = str(ROOT / 'review/s2-cache.sqlite')
from optomind_research.query_planner import QueryPlannerAgent
from optomind_research.runtime.upgrade3.plan_contract import load_plan
from optomind_research.runtime.upgrade3.candidate_corpus import SnippetTextStore, build_candidate_corpus, write_corpus
from optomind_research.s2_intelligence_gateway import S2IntelligenceGateway, S2Transport

class RealPlanner(QueryPlannerAgent):
    def _call_query_plan_model(self, prompt):
        result = TrialClient(model='qwen3.5-plus', lane='review-query').call([
            {'role': 'system', 'content': str(prompt.get('system_prompt') or '')},
            {'role': 'user', 'content': json.dumps(prompt.get('user_payload') or {}, ensure_ascii=False)},
        ], max_tokens=8192)
        self.last_raw_response = result['content']
        self.last_usage = result['_llm_usage']
        self.last_transport = {'fallback_used': False, 'transport_error': '', 'trial_call_id': result['call_id']}
        return self.last_raw_response

def main():
    out = ROOT / 'review'
    path = out / 'QUERY_PLAN_OUTCOME.json'
    protocol = json.loads((ROOT / 'TRIAL_PROTOCOL.json').read_text(encoding='utf-8'))
    planner = RealPlanner(real_llm=True, max_tokens=8192)
    if path.exists():
        previous = json.loads(path.read_text(encoding='utf-8'))
        repair_path = out / 'QUERY_PLAN_EXPLICIT_REPAIR-02.json'
        if previous.get('status') != 'invalid' or repair_path.exists():
            raise RuntimeError('Refusing implicit repeat of query planning')
        from optomind_research.runtime.upgrade3 import query_plan as QP
        # One coordinator-authorized semantic repair of the already recorded
        # successful response. The production facet-only retry cap is unchanged.
        details = QP.validate_plan(previous['plan'], fields=QP.load_field_dictionary(),
                                  original_question=protocol['review']['question'])['errors']
        prompt = QP.render_revision_prompt(QP.load_field_dictionary(), previous['plan'],
            details, user_question=protocol['review']['question'])
        raw = planner._call_query_plan_model(prompt)
        report = QP.validate_plan(json.loads(raw), fields=QP.load_field_dictionary(),
            original_question=protocol['review']['question'])
        outcome = {'original_question': protocol['review']['question'],
            'status': 'ok' if report['ok'] else 'invalid', 'plan': report['plan'],
            'degradation': {'degraded': not report['ok']}, 'errors': report['errors'],
            'round_usages': [planner.last_usage], 'explicit_coordinator_repair': True,
            'source_outcome': str(path), 'raw_response': raw}
        write_json(repair_path, outcome)
    else:
        outcome = planner.query_plan_v2(protocol['review']['question'])
        write_json(path, outcome)
    plan, receipt = load_plan(outcome, expected_question=protocol['review']['question'])
    write_json(out / 'FINAL_SCHEMA.json', plan)
    write_json(out / 'PLAN_RECEIPT.json', receipt)
    transport = S2Transport(cache_path=out / 's2-cache.sqlite', timeout_seconds=40, max_attempts=2)
    with_store = SnippetTextStore(out / 'snippets.sqlite')
    try:
        corpus = build_candidate_corpus(plan, gateway=S2IntelligenceGateway(transport=transport),
            per_query_limit=3, enrich=True, snippet_store=with_store, plan_receipt=receipt,
            checkpoint_callback=lambda value: write_corpus(out / 'CORPUS.checkpoint.json', value))
        write_corpus(out / 'CANDIDATE_CORPUS.json', corpus)
        print(json.dumps({'statistics': corpus['statistics'], 'query_statuses': [
            {k: row.get(k) for k in ('query_id','status','returned','query_text')} for row in corpus['query_audit']
        ]}, ensure_ascii=False))
    finally:
        with_store.close()

if __name__ == '__main__':
    main()
