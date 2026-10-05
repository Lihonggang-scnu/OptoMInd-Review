"""Offline BODY40 identity regression controls; no client makes network calls."""
import json
from pathlib import Path
import pytest
from optomind_research.runtime.upgrade3 import review_unit_writer as w

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / 'docs/acceptance/body40-20261005/writer/live'

class Replay:
    def __init__(self, response): self.response = response
    def __call__(self, messages, **kwargs): return self.response

def view(materials=None):
    materials = materials or [{'source_handle':'P0011','title':'Semiconductor defect spectroscopy'},
                              {'source_handle':'P0042','title':'Ceramic fatigue under cyclic loading'}]
    return w.UnitWritingView(chapter_id='C',unit_id='U',focus='offline',unit_index=1,unit_count=1,
        sibling_units=[],chapter_frame={},other_chapters=[],paragraph_tasks=[],table_tasks=[],
        materials=materials,sources={m['source_handle']:m for m in materials})

def run(body, **kwargs):
    return w.run_unit_writing(view(),client=Replay({'body_markdown':body}),planning_revision=True,**kwargs)

def archive_case(chapter, unit):
    directory = ARCHIVE / chapter / unit
    messages = json.loads((directory/'UNIT_MESSAGES.json').read_text())
    payload = json.loads(messages[-1]['content'])
    raw = json.loads(next((directory/'raw_responses').glob('*.raw')).read_text())
    return payload, raw

def test_real_ch6_raw_keeps_all_twelve_numeric_identities_unresolved():
    payload, raw = archive_case('Ch6','Ch6_Ch6_U4')
    body = w.parse_unit_body(raw)
    result = w.run_unit_writing(view(payload['sources']),client=Replay(raw),payload=payload,planning_revision=True)
    assert result['body_markdown'] == body
    assert '[P0011]' not in result['body_markdown']
    assert set(result['unresolved_numeric_citations']) == {f'[{i}]' for i in range(1,13)}
    assert result['numeric_citation_repairs'] == []
    assert result['citation_mapping_diagnostics'][0]['code'] == 'numeric_citation_generated_aliases_unconfirmed'

@pytest.mark.parametrize('body',['Optical finding [11].','Optical finding [1], mechanical finding [11].'])
def test_generated_suffixes_never_establish_output_dialect(body):
    assert run(body)['body_markdown'] == body

def test_caller_confirmation_keeps_conversion_and_provenance():
    result = run('Optical finding [1].',citation_number_map={'1':'P0011'})
    assert result['body_markdown'] == 'Optical finding [P0011].'
    assert result['numeric_citation_repairs'][0]['mapping_origin'] == 'caller_explicit'

@pytest.mark.parametrize('tail',[
    '[1] Semiconductor defect spectroscopy\n[1] Unknown unrelated reference',
    '[1] Semiconductor defect spectroscopy\n[1] Ceramic fatigue under cyclic loading',
])
def test_conflicting_or_unknown_duplicate_bibliography_is_not_unique(tail):
    body = 'Finding [1].\n\n'+tail
    result = run(body)
    assert result['body_markdown'] == body
    assert result['bibliography_title_citation_map'] == {}
    assert any(d['code']=='numeric_citation_mapping_ambiguous' for d in result['citation_mapping_diagnostics'])

def test_partial_title_evidence_does_not_mix_with_suffix_or_unresolved_numbers():
    body = 'Finding [1]. Other finding [11].\n\n[1] Ceramic fatigue under cyclic loading'
    assert run(body)['body_markdown'] == body

def test_unique_full_title_evidence_remains_supported():
    result = run('Finding [1].\n\n[1] Ceramic fatigue under cyclic loading')
    assert result['body_markdown'].count('[P0042]') == 2
    assert result['numeric_citation_repairs'][0]['mapping_origin'] == 'bibliography_exact_title'

def test_real_q01_reports_only_known_input_identifier_and_never_rewrites():
    payload, raw = archive_case('Ch7','Ch7_Ch7_U02')
    result = w.run_unit_writing(view(payload['sources']),client=Replay(raw),payload=payload,planning_revision=True)
    assert result['body_markdown'] == w.parse_unit_body(raw)
    assert result['body_markdown'].count('[Q01]') == 5
    assert result['non_source_identifier_citations'] == ['[Q01]']
    assert any(d['code']=='non_source_identifier_citation' and d['identifier']=='Q01' for d in result['citation_problems'])

def test_unknown_bracket_text_and_code_links_are_not_tool_problems():
    payload=w.unit_payload(view())
    payload['chapter_tool_materials']=[{'usable_content':json.dumps({'question_material':[{'question_id':'Q01'}]})}]
    body='Finding [Q01]. Unknown [Q99]. [control]. Code `[Q01]`. Link [Q01](https://example.invalid).'
    result=run(body,payload=payload)
    assert result['body_markdown']==body
    assert result['non_source_identifier_citations']==['[Q01]']
    assert run(body)['non_source_identifier_citations']==[]

def test_runtime_diagnostics_persist(tmp_path):
    result=run('Finding [11].')
    saved=w.write_unit_output(view(),result['body_markdown'],tmp_path,model='offline',language='en',mode='fake',
        used_messages=result['messages'],estimate={},citation_diagnostics=result)
    for field in ('numeric_citation_repairs','citation_number_map_origin','citation_mapping_diagnostics'):
        assert saved[field]==result[field]
    assert json.loads((tmp_path/'UNIT_RESULT.json').read_text())['citation_mapping_diagnostics']

@pytest.mark.parametrize('mapping,expected',[(None,'[11]'),({'11':'P0042'},'[P0042]')])
def test_completion_preserves_prefix_and_same_mapping_diagnostics(tmp_path,mapping,expected):
    item=view()
    item.paragraph_tasks=[{'paragraph_id':'B1','point':'Offline finding','source_uses':[{'source_handle':'P0011'},{'source_handle':'P0042'}]}]
    item.chapter_tool_materials=[{'usable_content':json.dumps({'question_material':[{'question_id':'Q01'}]}),
                                'unit_id':'U'}]
    body='Finding [11]. Tool [Q01].'
    result=w.run_unit_completion(item,existing_body='Existing exact prefix.  ',task_ids=['B1'],
        client=Replay({'content':json.dumps({'body_markdown':body,'status':'appended','covered_task_ids':['B1']})}),
        simulated=True,planning_revision=True,citation_number_map=mapping)
    assert result['completion_fragment'].endswith(body.replace('[11]',expected))
    assert result['non_source_identifier_citations'] == ['[Q01]']
    assert result['body_markdown'].startswith('Existing exact prefix.  ')
    report=w.write_unit_completion(item,result,tmp_path,estimate={},language='en')
    for field in ('numeric_citation_repairs','citation_mapping_diagnostics','citation_number_map_origin',
                  'bibliography_title_citation_map','non_source_identifier_citations'):
        assert report[field]==result[field]

def test_same_title_across_distinct_handles_is_ambiguous():
    materials=[{'source_handle':'P0011','title':'Identical full title'},
               {'source_handle':'P0042','title':'Identical full title'}]
    body='Finding [1].\n\n[1] Identical full title'
    result=w.run_unit_writing(view(materials),client=Replay({'body_markdown':body}),planning_revision=True)
    assert result['body_markdown']==body
    assert result['bibliography_title_citation_map']=={}

def test_repeated_identical_bibliography_definition_remains_unique():
    body='Finding [1].\n\n[1] Ceramic fatigue under cyclic loading\n[1] Ceramic fatigue under cyclic loading'
    assert run(body)['body_markdown'].count('[P0042]')==3

@pytest.mark.parametrize('title,other',[
    ('Na+ transport in α channels','Na- transport in α channels'),
    ('α phase transition','β phase transition'),
    ('x² transport coefficient','x2 transport coefficient'),
])
def test_title_identity_preserves_scientific_symbols(title,other):
    materials=[{'source_handle':'P0011','title':title}]
    body=f'Finding [1].\n\n[1] {other}'
    result=w.run_unit_writing(view(materials),client=Replay({'body_markdown':body}),planning_revision=True)
    assert result['body_markdown']==body
    assert result['bibliography_title_citation_map']=={}
