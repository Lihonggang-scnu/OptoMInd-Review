"""Offline synthetic model-boundary capture through production run/write paths."""
import argparse
import inspect
import hashlib
import subprocess
import tempfile
import json
from pathlib import Path
from optomind_research.runtime.upgrade3 import review_unit_writer as w

TABLE = '| Object | Condition | Result |\n|---|---|---|\n| A [P0002] | fixed input | bounded finding |'
PREFIX = 'Original paragraph [P0001].\r\nExact prefix stays.  '

def view():
    return w.UnitWritingView(chapter_id='CH_SYNTHETIC', unit_id='U1', focus='Synthetic output consumption',
        unit_index=1, unit_count=1, sibling_units=[], chapter_frame={}, other_chapters=[],
        paragraph_tasks=[{'paragraph_id':'P1', 'point':'Preserve synthetic prose', 'source_uses':[{'source_handle':'P0002'}]}],
        table_tasks=[{'table_id':'T1', 'purpose':'Compare synthetic objects at fixed input', 'columns':['Object','Condition','Result'],
            'row_tasks':[{'content':'A under fixed input', 'source_uses':[{'source_handle':'P0002'}]}]}],
        sources={'P0001':{'paper_id':'synthetic-1'},'P0002':{'paper_id':'synthetic-2'}},
        materials=[{'source_handle':h, 'study_summary_A':{'key_findings':'Synthetic bounded finding'}} for h in ('P0001','P0002')])

class Fake:
    def __init__(self, response): self.response=response; self.calls=[]
    def __call__(self, messages, **kwargs): self.calls.append(kwargs); return self.response

CASES = {
    'independent_table': {'body_markdown':'Synthetic prose [P0001].','table_markdown':TABLE},
    'duplicate_table': {'body_markdown':'Synthetic prose.\n\n'+TABLE,'table_markdown':TABLE},
    'metadata_only': {'body_markdown':'Synthetic prose remains.','table_tasks':[{'table_id':'T1','columns':['Object','Condition','Result']}]},
    'outer_markdown': {'body_markdown':'```markdown\n'+TABLE+'\n```'},
    'internal_code_table': {'body_markdown':'This is a code example, not the requested table.\n\n```markdown\n'+TABLE+'\n```'},
    'unknown_citation': {'body_markdown':'Synthetic finding [P9999].\n\n'+TABLE},
    'numeric_unmapped': {'body_markdown':'Synthetic finding [1].\n\n'+TABLE},
    'numeric_mapped': {'body_markdown':'Synthetic finding [1]. Code `[1]`; link [1](https://example.invalid).\n\n'+TABLE},
    'normal_control': {'body_markdown':'Unchanged synthetic prose [P0001].\n\n'+TABLE},
}

def capture(root):
    root=Path(root); root.mkdir(parents=True, exist_ok=True)
    summary={'classification':'SYNTHETIC: fixed fake model response, no model generation or quality claim',
        'paid_calls':0,'actual_config':{'client':'Fake', 'simulated_completion':True, 'ordinary_boundary_kwargs':{}, 'ordinary_tokens':'not supplied to fake boundary (no model executed)', 'ordinary_model_argument':'offline-synthetic', 'completion_model':'offline-synthetic', 'completion_output_tokens':4000,'completion_thinking_budget':0},'cases':{}}
    for name, fields in CASES.items():
        target=root/name; target.mkdir(exist_ok=True)
        response={'content':json.dumps({**fields,'status':'appended','covered_task_ids':['T1'],'issues':[]}), 'complete':True,'finish_reason':'stop'}
        (target/'RAW_FIXTURE.json').write_text(json.dumps(response,indent=2))
        payload=w.unit_payload(view())
        if name=='numeric_mapped': payload['citation_number_map']={'1':'P0002'}
        ordinary_client=Fake(response)
        run=w.run_unit_writing(view(),client=ordinary_client,model='offline-synthetic',payload=payload)
        w.write_unit_input(view(),run['messages'],target/'ordinary',estimate={},language='en')
        ordinary=w.write_unit_output(view(),run['body_markdown'],target/'ordinary',model='offline-synthetic',language='en',mode='fake',used_messages=run['messages'],estimate={},issues=run['issues'])
        extra={}
        if name=='numeric_mapped' and 'citation_number_map' in inspect.signature(w.run_unit_completion).parameters: extra['citation_number_map']={'1':'P0002'}
        completion_client=Fake(response)
        comp=w.run_unit_completion(view(),existing_body=PREFIX,task_ids=['T1'],client=completion_client,model='offline-synthetic',simulated=True,**extra)
        report=w.write_unit_completion(view(),comp,target/'completion',estimate={},language='en')
        summary['cases'][name]={'ordinary_body':run['body_markdown'],'ordinary_report':{k:ordinary.get(k) for k in ('unknown_citations','citation_problems','fence_report','output_consumption_status','content_review_status','issues')},
            'completion':{k:report.get(k) for k in ('pending','table_check','unknown_citations','citation_problems','fence_report','content_review_status','issues')},
            'actual_boundary_kwargs':{'ordinary':ordinary_client.calls,'completion':completion_client.calls},
            'prefix_preserved':(target/'completion/COMPLETED_BODY.md').read_bytes().startswith(PREFIX.encode())}
    (root/'SUMMARY.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))

def aggregate(root):
    """Keep actual Markdown/results/raw fixtures and one full message pair.

    Repeated input/message files are represented by verified hashes; capture()
    reproduces those files through the same production writers.
    """
    keep = {'RAW_FIXTURE.json', 'UNIT_BODY.simulated.md', 'UNIT_RESULT.json',
            'ORIGINAL_BODY.md', 'COMPLETION_FRAGMENT.md', 'COMPLETED_BODY.md',
            'COMPLETION_RESULT.json'}
    output = {'summary':json.loads((root/'SUMMARY.json').read_text()), 'artifacts':{},
              'omitted_repeated_input_messages_sha256':{}}
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.name == 'SUMMARY.json': continue
        relative = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        representative = relative in {
            'independent_table/ordinary/UNIT_MESSAGES.json',
            'independent_table/completion/COMPLETION_MESSAGES.json',
            'numeric_mapped/ordinary/UNIT_MESSAGES.json',
        }
        if path.name in keep or representative:
            output['artifacts'][relative] = {'sha256':digest,'utf8_content':raw.decode('utf-8')}
        else:
            output['omitted_repeated_input_messages_sha256'][relative] = digest
    return output


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('output', help='One compact aggregate JSON output')
    parser.add_argument('--baseline-commit', default='')
    args=parser.parse_args()
    source_path='optomind_research/runtime/upgrade3/review_unit_writer.py'
    if args.baseline_commit:
        source=subprocess.check_output(['git','show',args.baseline_commit+':'+source_path],text=True)
        keep={key:w.__dict__[key] for key in ('__name__','__package__','__file__','__spec__','__loader__')}
        w.__dict__.clear(); w.__dict__.update(keep)
        exec(compile(source,source_path,'exec'),w.__dict__)
    else:
        source=Path(source_path).read_text()
    with tempfile.TemporaryDirectory(prefix='body06-writer-') as directory:
        root=Path(directory)
        capture(root)
        output=aggregate(root)
        output['source']={'baseline_commit':args.baseline_commit or 'working_tree',
                          'writer_sha256':hashlib.sha256(source.encode()).hexdigest()}
        destination=Path(args.output); destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(json.dumps(output,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
