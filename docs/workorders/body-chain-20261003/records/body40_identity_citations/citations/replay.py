"""Read-only raw-response replay. Run at repository root with test bootstrap PYTHONPATH."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import types
from optomind_research.runtime.upgrade3 import review_unit_writer as current

ROOT=Path.cwd()
OUT=Path(__file__).resolve().parent
BASE='e0615b0f83ed001042b12c539a08a316f3ef6ab6'
source=subprocess.check_output(['git','show',f'{BASE}:optomind_research/runtime/upgrade3/review_unit_writer.py'],text=True)
baseline=types.ModuleType('optomind_research.runtime.upgrade3.body40_baseline_writer')
baseline.__package__='optomind_research.runtime.upgrade3'
baseline.__file__=current.__file__
sys.modules[baseline.__name__]=baseline
exec(compile(source,'baseline_review_unit_writer.py','exec'),baseline.__dict__)

class Replay:
    def __init__(self,response): self.response=response; self.calls=0
    def __call__(self,messages,**kwargs): self.calls+=1; return self.response

def sha(data): return hashlib.sha256(data).hexdigest()
def make_view(module,payload):
    materials=payload['sources']
    return module.UnitWritingView(chapter_id=payload['chapter_id'],unit_id=payload['unit_id'],focus=payload['unit_focus'],
        unit_index=1,unit_count=1,sibling_units=payload['sibling_units'],chapter_frame=payload['chapter_frame'],
        other_chapters=payload['other_chapters'],paragraph_tasks=payload['paragraph_tasks'],table_tasks=payload['table_tasks'],
        materials=materials,sources={m['source_handle']:m for m in materials},
        chapter_tool_materials=payload['chapter_tool_materials'])

summary={'scope':'Offline recorded-response consumption only; no new model, search or full BODY run',
         'baseline':BASE,'paid_calls':0,'network_calls':0,'cases':{}}
for chapter,unit in [('Ch6','Ch6_Ch6_U4'),('Ch7','Ch7_Ch7_U02')]:
    directory=ROOT/'docs/acceptance/body40-20261005/writer/live'/chapter/unit
    msgpath=directory/'UNIT_MESSAGES.json'
    rawpath=next((directory/'raw_responses').glob('*.raw'))
    messages=json.loads(msgpath.read_text())
    payload=json.loads(messages[-1]['content'])
    raw=json.loads(rawpath.read_text())
    rawbody=current.parse_unit_body(raw)
    before=baseline.run_unit_writing(make_view(baseline,payload),client=Replay(raw),payload=payload,planning_revision=True)
    after=current.run_unit_writing(make_view(current,payload),client=Replay(raw),payload=payload,planning_revision=True)
    assert before['messages']==after['messages'], 'Consumer repair must not change input messages'
    case={'raw_path':str(rawpath.relative_to(ROOT)),'raw_sha256':sha(rawpath.read_bytes()),
          'input_messages_path':str(msgpath.relative_to(ROOT)),'input_messages_sha256':sha(msgpath.read_bytes()),
          'before_after_sent_messages_equal':True,'raw_body_sha256':sha(rawbody.encode()),
          'before':{},'after':{}}
    for label,result in [('before',before),('after',after)]:
        body=result['body_markdown']
        (OUT/f'{unit}.{label}.md').write_text(body)
        case[label]={'body_sha256':sha(body.encode()),'body_equals_parsed_raw':body==rawbody,
                     'P0011_count':body.count('[P0011]'),'Q01_count':body.count('[Q01]')}
        for field in ('numeric_citation_repairs','bibliography_title_citation_map','citation_number_map_origin',
                      'citation_mapping_diagnostics','unknown_citations','unresolved_numeric_citations',
                      'known_tool_identifiers','non_source_identifier_citations','citation_problems'):
            case[label][field]=result.get(field)
    summary['cases'][unit]=case
(OUT/'REAL_REPLAY_BEFORE_AFTER.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({unit:{'before_equals_raw':case['before']['body_equals_parsed_raw'],
                            'after_equals_raw':case['after']['body_equals_parsed_raw'],
                            'before_repairs':case['before']['numeric_citation_repairs'],
                            'after_repairs':case['after']['numeric_citation_repairs'],
                            'after_non_source_identifiers':case['after']['non_source_identifier_citations']}
                  for unit,case in summary['cases'].items()},ensure_ascii=False,indent=2))
