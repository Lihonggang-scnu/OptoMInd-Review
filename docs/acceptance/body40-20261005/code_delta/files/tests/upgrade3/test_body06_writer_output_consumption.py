"""Bounded WO06 controls: synthetic model boundary, actual run/write functions."""
import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import pytest
from optomind_research.runtime.upgrade3 import review_unit_writer as w

ROOT = Path(__file__).resolve().parents[2]
CAPTURE = ROOT / 'docs/workorders/body-chain-20261003/records/body06/writer/capture.py'
spec = importlib.util.spec_from_file_location('body06_fixture', CAPTURE)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
TABLE = fixture.TABLE


def ordinary(tmp_path, response, *, view=None, payload=None, mapping=None):
    view = view or fixture.view()
    client = fixture.Fake(response)
    result = w.run_unit_writing(view, client=client, model='offline-synthetic', payload=payload, citation_number_map=mapping)
    report = w.write_unit_output(view, result['body_markdown'], tmp_path,
        model='offline-synthetic',language='en',mode='fake',used_messages=result['messages'],estimate={},issues=result['issues'])
    return result, report, client


@pytest.mark.parametrize('wrap', [lambda x:x, lambda x:json.dumps(x), lambda x:{'content':json.dumps(x)},
    lambda x:{'choices':[{'message':{'content':json.dumps(x)}}]}, lambda x:{'response':x},
    lambda x:'```json\n'+json.dumps(x)+'\n```'])
def test_independent_table_survives_all_provider_shapes(wrap):
    assert w.parse_unit_body(wrap({'body_markdown':'Prose.', 'table_markdown':TABLE})) == 'Prose.\n\n'+TABLE


def test_dedup_finished_table_even_if_cell_spacing_differs():
    spaced = TABLE.replace('|', '| ')
    body = 'Before\n\n'+TABLE+'\n\nAfter'
    assert w.parse_unit_body({'body_markdown':body,'table_markdown':spaced}) == body


def test_table_only_is_usable_but_specification_is_not():
    assert w.parse_unit_body({'table_markdown':TABLE}) == TABLE
    with pytest.raises(w.UnitWritingError):
        w.parse_unit_body({'table_tasks':[{'table_id':'T1','columns':['a','b']}]})
    assert w.parse_unit_body({'body_markdown':'Existing prose.', 'table_tasks':[{'content':TABLE}]}) == 'Existing prose.'


@pytest.mark.parametrize('fence', ['```markdown','```md','~~~markdown','````markdown'])
def test_unwrap_single_outer_markdown_fence(fence):
    assert w.parse_unit_body(fence+'\n'+TABLE+'\n'+fence.split('m')[0]) == TABLE


def test_outer_wrapper_preserves_shorter_inner_code_fence():
    body = 'Prose.\n\n```python\nx = [1]\n```\n\n'+TABLE
    assert w.parse_unit_body('````markdown\n'+body+'\n````') == body


@pytest.mark.parametrize('body', ['```python\nx = [1]\n```', '```\nx = [1]\n```',
    'Prose.\n\n```json\n{"body_markdown":"code example"}\n```',
    '```json\n{"body_markdown":"code example"}\n```'])
def test_actual_code_examples_are_not_unwrapped(body):
    assert w.parse_unit_body({'body_markdown':body}) == body


@pytest.mark.parametrize('body', ['Prose.\n\n```markdown\n'+TABLE+'\n```',
    '~~~text\n'+TABLE+'\n~~~', '\n'.join('    '+line for line in TABLE.splitlines()),
    '| a | b |\n\n|---|---|\n| x | y |', '| a | b |\n|---|---|\n| x | y | z |'])
def test_code_or_malformed_table_is_not_a_finished_table(body):
    assert not w._markdown_table_check(body)['valid']


def test_ordinary_missing_table_preserves_body_and_pending_report(tmp_path):
    result, report, _ = ordinary(tmp_path, {'body_markdown':'Useful prose.', 'table_tasks':[{'table_id':'T1'}]})
    assert result['body_markdown'] == 'Useful prose.'
    assert report['output_consumption_status'] == 'pending_table'
    assert report['complete'] is True  # transport, never content acceptance
    assert report['content_review_status'] == 'not_reviewed'
    assert {'code':'markdown_table_missing_or_invalid'} in report['issues']


def test_ordinary_unknown_handles_and_fences_persist(tmp_path):
    body = 'Unknown [P9999][P0002].\n\n```python\nx = 1\n```\n\n'+TABLE
    result, report, _ = ordinary(tmp_path, {'body_markdown':body})
    assert result['unknown_citations'] == report['unknown_citations'] == ['P9999']
    assert report['fence_report']['remaining_fenced_lines']
    assert json.loads((tmp_path/'UNIT_RESULT.json').read_text())['unknown_citations'] == ['P9999']
    assert not result['issues']  # reporting is not a blanket block of usable prose


@pytest.mark.parametrize('mapping,expected', [(None,'[1]'), ({'1':'P0002'},'[P0002]'), ({'1':'P9999'},'[1]')])
def test_numeric_citations_need_caller_mapping_not_handle_suffix(tmp_path, mapping, expected):
    result, report, _ = ordinary(tmp_path, {'body_markdown':'Finding [1].\n\n'+TABLE,
        'citation_number_map':{'1':'P0001'}}, mapping=mapping)
    assert result['body_markdown'].startswith('Finding '+expected+'.')
    assert report['unresolved_numeric_citations'] == ([] if expected=='[P0002]' else ['[1]'])


def test_caller_payload_mapping_and_link_code_preservation(tmp_path):
    body = ('Finding [1, 2]. Code `[1]`. Link [1](https://example.invalid). Reference [label][1].\n'
            '[1]: https://example.invalid\n\n```python\nx = [1]\n```\n\n'+TABLE)
    payload = w.unit_payload(fixture.view()); payload['citation_number_map']={'1':'P0002','2':'P0001'}
    result, _, _ = ordinary(tmp_path, {'body_markdown':body}, payload=payload)
    assert result['body_markdown'] == body.replace('Finding [1, 2].','Finding [P0002][P0001].')


def test_planning_revision_local_aliases_are_explicit_and_ambiguous_aliases_stay_unmapped(tmp_path):
    mapping = w._local_numeric_citation_map(['P0001', 'P0002', 'P0057', 'P00057'])
    assert mapping['1'] == 'P0001'
    assert mapping['2'] == 'P0002'
    assert '57' not in mapping
    assert mapping['0057'] == 'P0057'

    payload = w.unit_payload(fixture.view())
    payload['planning_revision_mode'] = True
    payload['citation_number_map'] = w._local_numeric_citation_map(['P0001', 'P0002'])
    result, _, _ = ordinary(tmp_path, {'body_markdown':'Finding [1].\n\n'+TABLE}, payload=payload)
    assert result['body_markdown'].startswith('Finding [P0001].')


def test_planning_revision_run_declares_and_consumes_same_local_map():
    response = {'body_markdown':'Finding [1].\n\n'+TABLE, 'complete':True, 'finish_reason':'stop'}
    client = fixture.Fake(response)
    result = w.run_unit_writing(fixture.view(), client=client, model='offline-synthetic',
                                planning_revision=True)
    sent = json.loads(result['messages'][-1]['content'])
    assert sent['citation_number_map']['1'] == 'P0001'
    assert result['body_markdown'].startswith('Finding [P0001].')

    explicit_empty = w.unit_payload(fixture.view(), planning_revision=True,
                                     citation_number_map={})
    preserved = w.run_unit_writing(fixture.view(), client=fixture.Fake(response),
                                   model='offline-synthetic', payload=explicit_empty,
                                   planning_revision=True)
    assert json.loads(preserved['messages'][-1]['content'])['citation_number_map'] == {}
    assert preserved['body_markdown'].startswith('Finding [1].')

    explicit = w.run_unit_writing(
        fixture.view(), client=fixture.Fake(response), model='offline-synthetic',
        planning_revision=True, citation_number_map={'1': 'P0002'})
    assert json.loads(explicit['messages'][-1]['content'])['citation_number_map'] == {'1': 'P0002'}
    assert explicit['body_markdown'].startswith('Finding [P0002].')


def test_exact_local_bibliography_title_overrides_generated_alias_but_not_custom_map():
    title = 'Microbial metabolic pathways guide response to immune checkpoint blockade therapy.'
    base = fixture.view()
    materials = [
        {'source_handle': 'P0001', 'title': 'Unrelated local source',
         'study_summary_A': {'key_findings': 'local'}},
        {'source_handle': 'P0002', 'title': 'Synthetic source',
         'study_summary_A': {'key_findings': 'synthetic'}},
        {'source_handle': 'P0444', 'title': title,
         'study_summary_A': {'key_findings': 'exact title'}}]
    view = replace(base, materials=materials,
                   sources={item['source_handle']: item for item in materials})
    body = f'Finding [1].\n\n' + TABLE + f'\n\n[1] {title}'
    automatic = w.run_unit_writing(
        view, client=fixture.Fake({'body_markdown': body, 'complete': True,
                                   'finish_reason': 'stop'}),
        model='offline-synthetic', planning_revision=True)
    assert automatic['body_markdown'].count('[P0444]') == 2
    assert automatic['body_markdown'].count('[P0001]') == 0
    assert automatic['bibliography_title_citation_map'] == {'1': 'P0444'}

    custom = w.run_unit_writing(
        view, client=fixture.Fake({'body_markdown': body, 'complete': True,
                                   'finish_reason': 'stop'}),
        model='offline-synthetic', planning_revision=True,
        citation_number_map={'1': 'P0001'})
    assert custom['body_markdown'].count('[P0001]') == 2
    assert custom['body_markdown'].count('[P0444]') == 0

    empty_payload = w.unit_payload(view, planning_revision=True,
                                    citation_number_map={})
    empty = w.run_unit_writing(
        view, client=fixture.Fake({'body_markdown': body, 'complete': True,
                                   'finish_reason': 'stop'}),
        model='offline-synthetic', payload=empty_payload,
        planning_revision=True)
    assert empty['body_markdown'].count('[1]') == 2
    assert empty['bibliography_title_citation_map'] == {'1': 'P0444'}

    unmarked_payload = dict(w.unit_payload(
        view, planning_revision=True, citation_number_map={'1': 'P0001'}))
    unmarked_payload.pop('citation_number_map_origin', None)
    unmarked = w.run_unit_writing(
        view, client=fixture.Fake({'body_markdown': body, 'complete': True,
                                   'finish_reason': 'stop'}),
        model='offline-synthetic', payload=unmarked_payload,
        planning_revision=True)
    assert unmarked['body_markdown'].count('[P0001]') == 2
    assert unmarked['body_markdown'].count('[P0444]') == 0


def test_completion_independent_table_original_bytes_and_report(tmp_path):
    client = fixture.Fake({'body_markdown':'New finding [1][P9999].', 'table_markdown':TABLE,
        'status':'appended', 'complete':True})
    result = w.run_unit_completion(fixture.view(),existing_body=fixture.PREFIX,task_ids=['T1'],client=client,
        model='offline-synthetic',citation_number_map={'1':'P0002'})
    assert json.loads(result['messages'][-1]['content'])['citation_number_map'] == {'1': 'P0002'}
    assert not result['pending']
    assert result['body_markdown'].startswith(fixture.PREFIX)
    assert result['unknown_citations'] == ['P9999']
    assert result['citation_scope'] == 'completion_fragment'
    assert '[P0001]' not in result['unknown_citations']
    assert 'New finding [P0002][P9999].' in result['completion_fragment']
    report = w.write_unit_completion(fixture.view(),result,tmp_path,estimate={},language='en')
    assert report['unknown_citations'] == ['P9999']
    assert report['content_review_status'] == 'not_reviewed'
    assert (tmp_path/'COMPLETED_BODY.md').read_bytes().startswith(fixture.PREFIX.encode())
    assert (tmp_path/'ORIGINAL_BODY.md').read_bytes() == fixture.PREFIX.encode()


def test_completion_internal_code_table_is_pending_but_candidate_is_retained(tmp_path):
    body = 'An example.\n\n```markdown\n'+TABLE+'\n```'
    result = w.run_unit_completion(fixture.view(),existing_body=fixture.PREFIX,task_ids=['T1'],
        client=fixture.Fake({'body_markdown':body,'status':'appended','complete':True}))
    assert result['pending']
    assert result['body_markdown'] == fixture.PREFIX
    assert result['completion_fragment'] == body
    assert result['fence_report']['remaining_fenced_lines']
    report = w.write_unit_completion(fixture.view(),result,tmp_path,estimate={},language='en')
    assert report['fence_report']['remaining_fenced_lines']


def test_completion_outer_wrapper_unwrapped_without_unresolved_issue():
    result = w.run_unit_completion(fixture.view(),existing_body=fixture.PREFIX,task_ids=['T1'],
        client=fixture.Fake({'body_markdown':'```markdown\n'+TABLE+'\n```','status':'appended','complete':True}))
    assert not result['pending']
    assert result['completion_fragment'] == TABLE
    assert result['fence_report']['remaining_fenced_lines'] == []
    assert not result['issues']
    assert result['output_normalization']


def test_unaffected_real_acceptance_body_is_byte_identical():
    for filename in ('BODY04_WRITTEN_BODY.md','BODY05_WRITTEN_BODY.md'):
        body = (ROOT/'docs/acceptance/body05-real-20261004'/filename).read_text()
        assert w.parse_unit_body({'body_markdown':body}) == body.strip()


def test_historical_malformed_json_recovery_is_preserved():
    assert w.parse_unit_body('{"body_markdown":"A "quoted" term.\\n\\sim", "issues":[]}') == 'A "quoted" term.\n\\sim'


def test_dedup_ignores_separator_alignment_and_width():
    variant = TABLE.replace('|---|---|---|', '|:-----|---:|:----:|')
    assert w.parse_unit_body({'body_markdown':TABLE,'table_markdown':variant}) == TABLE


@pytest.mark.parametrize('protected', [
    '[label](https://example.invalid/[1])', '[label](https://example.invalid "[1] title")',
    '![figure](images/[1].png)', '[label]: https://example.invalid/[1]',
    'Example ``[1]\n[2]``', '```python\n[1]\n```', '[label][1]',
])
def test_numeric_repair_preserves_whole_code_and_link_constructs(protected):
    text = protected+'\n\nFinding [1].'
    repaired, changes = w._repair_numeric_citations(text, {'1':'P0002','2':'P0001'}, ['P0001','P0002'])
    assert repaired == protected+'\n\nFinding [P0002].'
    assert len(changes) == 1


def test_unreadable_task_only_response_retains_raw_before_failing(tmp_path):
    response = {'content':json.dumps({'table_tasks':[{'table_id':'T1'}]}), 'complete':True}
    with pytest.raises(w.UnitWritingError,match='unit_body_empty_or_unreadable'):
        w.run_unit_writing(fixture.view(),client=fixture.Fake(response),raw_response_dir=tmp_path)
    raw = list(tmp_path.glob('*.raw'))
    assert len(raw) == 1
    assert json.loads(raw[0].read_text()) == response


@pytest.mark.parametrize('body', [
    '    print(1)\n    print(2)',
    '\n'.join('    '+line for line in TABLE.splitlines()),
    '\n'.join('    '+line for line in ('```markdown\n'+TABLE+'\n```').splitlines()),
])
def test_parse_preserves_leading_indented_code(body):
    assert w.parse_unit_body({'body_markdown':body}) == body
    assert not w._markdown_table_check(w.parse_unit_body(body))['valid']


@pytest.mark.parametrize('protected', [
    'Link [1][2].\n\n[2]: https://example.invalid',
    'Link [1][].\n\n[1]: https://example.invalid',
    'Link [1].\n\n[1]: https://example.invalid',
    '[label](https://example.invalid/foo(bar) "[1] title")',
])
def test_defined_numeric_links_and_nested_destinations_are_preserved(protected):
    repaired, changes = w._repair_numeric_citations(protected, {'1':'P0001','2':'P0002'}, ['P0001','P0002'])
    assert repaired == protected
    assert changes == []


def test_missing_table_report_reaches_existing_delivery_pending_gate(tmp_path):
    from scripts.upgrade3.full_review_draft import _unit_pending_problem
    _, report, _ = ordinary(tmp_path, {'body_markdown':'Useful prose.', 'table_tasks':[{'table_id':'T1'}]})
    persisted = json.loads((tmp_path/'UNIT_RESULT.json').read_text())
    assert persisted == report
    assert _unit_pending_problem(persisted) == 'writer_issues:1'


@pytest.mark.parametrize('body', [
    'See [methods](https://example.invalid). New result [P9999] under fixed conditions (A).',
    'New result [P9999] (condition).',
    'Result [P9999]: reported under fixed conditions.',
])
def test_link_mask_and_spaced_parenthetical_do_not_hide_unknown_handle(tmp_path, body):
    assert w.citations_in(body) == ['P9999']
    _, report, _ = ordinary(tmp_path, {'body_markdown':body+'\n\n'+TABLE})
    assert report['unknown_citations'] == ['P9999']
