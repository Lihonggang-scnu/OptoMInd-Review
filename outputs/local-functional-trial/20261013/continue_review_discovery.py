import hashlib
import json
from trial_transport import ROOT, write_json
from run_review_discovery import S2Transport, S2IntelligenceGateway, SnippetTextStore, build_candidate_corpus, write_corpus, load_plan
from optomind_research.runtime.upgrade3.query_plan import validate_plan, load_field_dictionary

out = ROOT / 'review'
source = out / 'QUERY_PLAN_EXPLICIT_REPAIR-02.json'
record = json.loads(source.read_text(encoding='utf-8'))
envelope = json.loads(record['raw_response'])
question = json.loads((ROOT / 'TRIAL_PROTOCOL.json').read_text(encoding='utf-8'))['review']['question']
if set(envelope) != {'user_question', 'previous_plan'} or envelope['user_question'] != question:
    raise ValueError('Only the exact explicit model envelope is extractable')
report = validate_plan(envelope['previous_plan'], fields=load_field_dictionary(), original_question=question)
if not report['ok']:
    raise ValueError(json.dumps(report['errors']))
outcome = {'status': 'ok', 'plan': report['plan'], 'original_question': question,
           'degradation': {'degraded': False}, 'round_usages': record['round_usages'],
           'extraction_path': '$.previous_plan', 'source_response_sha256': hashlib.sha256(record['raw_response'].encode()).hexdigest()}
write_json(out / 'QUERY_PLAN_CANONICAL_RESPONSE.json', outcome)
plan, receipt = load_plan(outcome, expected_question=question)
write_json(out / 'FINAL_SCHEMA.json', plan)
write_json(out / 'PLAN_RECEIPT.json', receipt)
transport = S2Transport(cache_path=out / 's2-cache.sqlite', timeout_seconds=25, max_attempts=2)
store = SnippetTextStore(out / 'snippets.sqlite')
try:
    corpus = build_candidate_corpus(plan, gateway=S2IntelligenceGateway(transport=transport),
        per_query_limit=3, enrich=True, snippet_store=store, plan_receipt=receipt,
        checkpoint_callback=lambda value: write_corpus(out / 'CORPUS.checkpoint.json', value))
    write_corpus(out / 'CANDIDATE_CORPUS.json', corpus)
    print(json.dumps(corpus['statistics'], ensure_ascii=False))
finally:
    store.close()
