import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(r'F:\OptoMind-Review-2\outputs\writer_candidates_local_20261007_60cny')
LIVE = ROOT / 'chapter_live'
ARRANGEMENT = Path(r'F:\OptoMind-Review-2\outputs\on_demand_promotion_local_20261007_10cny\arrangement_paid\Ch2\CHAPTER_ARRANGEMENT.json')
VIEW = Path(r'F:\OptoMind-Review-2\outputs\on_demand_promotion_local_20261007_10cny\arrangement_paid\Ch2\ARRANGEMENT_INPUT.json')
RAW = LIVE / 'stages' / 'writer_chapter' / '1ecb76a1c97d3064daac392fe29c0f8db53ff258cad6faa7d81ab27836b94d0d' / 'attempt_001' / 'RAW_RESPONSE.json'
ORIGINAL_RESULT = RAW.parent / 'RESULT.json'
OUT = LIVE / 'format_repair'

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def dump(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')

from optomind_research.runtime.upgrade3.writer_candidates_contracts import build_chapter_input, parse_candidate_response

raw = json.loads(RAW.read_text(encoding='utf-8'))
chapter = build_chapter_input(ARRANGEMENT, view_path=VIEW)
response = {
    'content': raw.get('content', ''),
    'complete': raw.get('complete'),
    'finish_reason': raw.get('finish_reason'),
    'request_id': raw.get('request_id'),
}
recovered = parse_candidate_response(response, chapter)
repair = list(recovered.get('diagnostics', {}).get('format_repair') or [])
if not repair:
    raise SystemExit('format_repair_not_applied')
original = json.loads(ORIGINAL_RESULT.read_text(encoding='utf-8'))
body = recovered.get('body_markdown') or ''
old_body = original.get('body_markdown') or ''
report = {
    'schema_version': 'optomind.writer_candidates.format_repair_replay.v1',
    'replay_mode': 'offline_saved_response_only',
    'adoption_status': 'proposed_for_root_review',
    'model_calls': 0,
    'ledger_writes': 0,
    'scientific_text_edits': 0,
    'source': {
        'raw_response_path': str(RAW),
        'raw_response_file_sha256': sha(RAW),
        'raw_stream_path': raw.get('raw_stream_path'),
        'raw_stream_sha256': raw.get('raw_stream_sha256'),
        'raw_response_record_sha256': raw.get('raw_response_sha256'),
        'original_result_path': str(ORIGINAL_RESULT),
        'original_result_file_sha256': sha(ORIGINAL_RESULT),
        'run_id': '20261007T115231Z-f1a78a9f11',
        'call_id': raw.get('call_id'),
        'provider_request_id': raw.get('request_id'),
        'reservation_id': 'res-a427d185cd084c4c',
        'actual_cost_cny': 0.1378104,
    },
    'input': {
        'arrangement_path': str(ARRANGEMENT),
        'view_path': str(VIEW),
        'chapter_id': chapter.get('chapter_id'),
        'task_count': len(recovered.get('pending_task_ids', [])) + len(recovered.get('diagnostics', {}).get('declared_covered_task_ids', [])),
        'source_count': len(chapter.get('sources') or []),
    },
    'transport': {
        'complete': raw.get('complete'),
        'finish_reason': raw.get('finish_reason'),
        'status_code': raw.get('status_code'),
        'usage': raw.get('usage'),
    },
    'repair': repair,
    'validation': {
        'complete': recovered.get('complete'),
        'pending_task_ids': recovered.get('pending_task_ids'),
        'block_count': len(recovered.get('blocks') or []),
        'body_char_count': len(body),
        'old_partial_body_prefix_match': bool(old_body) and body.startswith(old_body),
        'table_checks': recovered.get('diagnostics', {}).get('table_checks'),
        'unknown_citations': recovered.get('diagnostics', {}).get('unknown_citations'),
        'unresolved_numeric_citations': recovered.get('diagnostics', {}).get('unresolved_numeric_citations'),
        'non_source_identifier_citations': recovered.get('diagnostics', {}).get('non_source_identifier_citations'),
        'semantic_quality_unreviewed': recovered.get('semantic_quality_unreviewed'),
        'content_review_status': recovered.get('diagnostics', {}).get('content_review_status'),
    },
    'preservation': {
        'original_raw_response_preserved': True,
        'original_result_preserved': True,
        'adopted_body_is_parser_output_only': True,
        'recovery_does_not_claim_new_model_run': True,
    },
}
dump(OUT / 'FORMAT_REPAIR_REPORT.json', report)
dump(OUT / 'ADOPTED_CHAPTER_RESULT_PROPOSAL.json', recovered)
(OUT / 'ADOPTED_CHAPTER_BODY_PROPOSAL.md').write_text(body, encoding='utf-8')
dump(OUT / 'ADOPTION_RECORD.json', {
    'schema_version': 'optomind.writer_candidates.adoption_record.v1',
    'status': 'proposed_for_root_review',
    'source_raw_response_sha256': raw.get('raw_response_sha256'),
    'source_raw_stream_sha256': raw.get('raw_stream_sha256'),
    'repaired_result_sha256': sha(OUT / 'ADOPTED_CHAPTER_RESULT_PROPOSAL.json'),
    'repaired_body_sha256': sha(OUT / 'ADOPTED_CHAPTER_BODY_PROPOSAL.md'),
    'format_repair_report': str(OUT / 'FORMAT_REPAIR_REPORT.json'),
    'model_calls': 0,
    'new_paid_calls': 0,
    'adoption_requires_root_review': True,
})
print(json.dumps({
    'status': 'proposed_for_root_review',
    'complete': recovered.get('complete'),
    'blocks': len(recovered.get('blocks') or []),
    'pending_task_count': len(recovered.get('pending_task_ids') or []),
    'body_chars': len(body),
    'format_repairs': repair,
    'old_partial_body_prefix_match': bool(old_body) and body.startswith(old_body),
    'unknown_citations': recovered.get('diagnostics', {}).get('unknown_citations'),
    'unresolved_numeric_citations': recovered.get('diagnostics', {}).get('unresolved_numeric_citations'),
    'output': str(OUT),
}, ensure_ascii=False, indent=2))
