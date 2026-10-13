"""Collect auditable trial evidence without starting any provider or solver."""
import hashlib
import importlib.metadata as metadata
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from trial_transport import ROOT, LEDGER, SOURCE, write_json

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))
def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()

products = []
for name, directory in [('Article', 'OptoMind-Article'), ('Review', 'OptoMInd-Review'), ('VeriTMM', 'VeriTMM')]:
    repo = Path('D:/OptoMind-Article-3/repos') / directory
    products.append({'product': name, 'repository': str(repo), 'head': git(repo, 'rev-parse', 'HEAD'),
        'tree': git(repo, 'rev-parse', 'HEAD^{tree}'), 'branch': git(repo, 'branch', '--show-current'),
        'working_tree_clean': not git(repo, 'status', '--porcelain'), 'pushed_by_this_task': False})
budget = LEDGER.as_dict()
corpus = read(ROOT / 'review/CANDIDATE_CORPUS.json')
query_status = {}
for row in corpus['query_audit']:
    query_status[row['status']] = query_status.get(row['status'], 0) + 1
plan_file = ROOT / 'review/planning-02/PROGRESSIVE_REVIEW_PLAN.partial.json'
plan_state_file = plan_file.parent / 'RUN_STATE.json'
partial_plan = read(plan_file) if plan_file.exists() else {}
outline = partial_plan.get('shared_outline') or {}
chapters = outline.get('chapters') or [] if isinstance(outline, dict) else outline
artifacts = [ROOT / 'TESTED_COMMITS.json', ROOT / 'TRIAL_PROTOCOL.json', ROOT / 'article/NUMERIC_ACCEPTANCE.json',
    ROOT / 'article/INDEPENDENT_SPECTRUM.csv', ROOT / 'review/CANDIDATE_CORPUS.json', ROOT / 'review/POOL_RESULT.json',
    ROOT / 'review/docling-basic/BATCH_RESULT.json', ROOT / 'history-audit/HISTORICAL_BINDING_COVERAGE.json']
artifacts += [ROOT / 'TRIAL_REPORT.md', ROOT / 'WORK_LOG.md',
    ROOT / 'docling-integration/DOC_LING_RUNTIME_ACCEPTANCE.json',
    ROOT / 'review/evidence-audit/NEW_PILOT_EVIDENCE_AUDIT.json']
if plan_file.exists(): artifacts.append(plan_file)
report = {
    'generated_utc': datetime.now(timezone.utc).isoformat(), 'repositories': products,
    'preserved_tested_commits': read(ROOT / 'TESTED_COMMITS.json'),
    'budget': {'limit_cny': budget['limit_cny'], 'usage_price_estimate_cny': budget['actual_cny'],
        'held_cny': budget['reserved_cny'], 'physical_attempts': len(budget['reservations']),
        'unresolved': [row['call_id'] for row in budget['reservations'] if row['status'] != 'settled'],
        'basis': 'Provider usage times official China regional list price; not an account invoice',
        'automatic_transport_retries': 0, 'key_slots_per_attempt': 1,
        'transport_source': str(SOURCE), 'transport_source_sha256': sha(SOURCE)},
    'review': {'retrieved_distinct_papers': corpus['statistics']['distinct_papers'],
        'retrieval_hits': corpus['statistics']['raw_hits'], 'query_status_counts': query_status,
        'new_fulltext_cards': 2, 'scope': 'small supervised pilot, phase level2 only, no BODY',
        'planning_state': read(plan_state_file) if plan_state_file.exists() else None,
        'final_shared_outline_chapter_count': len(chapters),
        'chapter_proposal_count': len(list((plan_file.parent / 'stages/chapter_proposals').glob('*.json'))),
        'acceptance': 'not accepted overall: final shared outline lost chapter list; evidence insufficiency and unsupported claims remain',
        'query_planner_limit': 'Global requirement mapping needed explicit model repair and exact response-envelope extraction; original failures retained'},
    'article': read(ROOT / 'article/NUMERIC_ACCEPTANCE.json'),
    'historical_replay': 'incomplete: no source-bound fourth writer response; independent of new pilot',
    'open_items': ['Gradient adapter ignores finite forward cap; reserved64/actual168 failure retained. Pilot contained by 8-step single-start Adam; production cap needs repair.',
        'Article feedback must preserve original layer-count charter before compile/execute; three-layer change was blocked by trial guard.',
        'Arithmetic sample mean differs from existing trapezoidal band mean; metric convention must be explicit before comparison.',
        'Review does not establish primary experimental efficiency tradeoffs or complete semantic retrieval constraints.',
        'Review finalized outline has zero chapters despite three saved chapter proposals; final language includes unsupported certainty and a substituted source title.',
        'Full chapter arrangement/writing/publication and full regression were not run in this pilot.'],
    'comparison': {'performed': False, 'reason': 'Overall Article acceptance incomplete; do not infer upgrade benefit from the verified first spectrum'},
    'artifacts': [{'path': str(path), 'sha256': sha(path)} for path in artifacts],
}
write_json(ROOT / 'FINAL_TRIAL_ACCEPTANCE.json', report)
write_json(ROOT / 'BUDGET_STATUS.json', budget)
print(json.dumps({'cost_cny': budget['actual_cny'], 'holds_cny': budget['reserved_cny'],
    'repositories_clean': all(row['working_tree_clean'] for row in products),
    'review_state': report['review']['planning_state']['status'] if report['review']['planning_state'] else None}))
