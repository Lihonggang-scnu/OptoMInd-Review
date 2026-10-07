import hashlib, json, subprocess
from pathlib import Path
out=Path(r'F:\OptoMind-Review-2\outputs\writer_candidates_local_20261007_60cny\chapter_live\format_repair')
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
proposal=out/'ADOPTED_CHAPTER_RESULT_PROPOSAL.json'
body=out/'ADOPTED_CHAPTER_BODY_PROPOSAL.md'
report=out/'FORMAT_REPAIR_REPORT.json'
raw=Path(r'F:\OptoMind-Review-2\outputs\writer_candidates_local_20261007_60cny\chapter_live\stages\writer_chapter\1ecb76a1c97d3064daac392fe29c0f8db53ff258cad6faa7d81ab27836b94d0d\attempt_001\RAW_RESPONSE.json')
pointer={
  'schema_version':'optomind.writer_candidates.adopted_pointer.v1',
  'status':'root_accepted_format_repaired_A',
  'selection':'A',
  'adoption_kind':'offline_format_repair_of_original_paid_response',
  'new_model_calls':0,
  'new_paid_calls':0,
  'ledger_writes':0,
  'root_approval':'explicit parent approval after review',
  'source':{
    'raw_response_path':str(raw),
    'raw_response_file_sha256':sha(raw),
    'run_id':'20261007T115231Z-f1a78a9f11',
    'call_id':'writer_chapter-1ecb76a1c97d-attempt_001',
    'provider_request_id':'chatcmpl-b5543f04-d299-914b-82e8-a5c045e60bb1',
    'reservation_id':'res-a427d185cd084c4c',
    'actual_cost_cny':0.1378104,
  },
  'adopted':{
    'result_path':str(proposal),
    'result_sha256':sha(proposal),
    'body_path':str(body),
    'body_sha256':sha(body),
    'format_repair_report':str(report),
    'format_repair_report_sha256':sha(report),
  },
  'preservation':{
    'original_paid_raw_unchanged':True,
    'original_pending_result_unchanged':True,
    'adoption_is_pointer_only':True,
    'semantic_quality_unreviewed':True,
  },
  'code_commit':'14257f4274e1deeb77aa6d125447bfb2a5d4c7d3',
}
path=out/'ADOPTED_A_POINTER.json'
path.write_text(json.dumps(pointer,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(pointer,ensure_ascii=False,indent=2))
