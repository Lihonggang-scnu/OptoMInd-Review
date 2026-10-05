"""Independent R2 actual-payload, budget and checkpoint controls.

Invented engineering text; real SQLite/FTS and production adapter/messages.
Only model construction/invocation and network boundaries are substituted.
"""
import json
import socket
import pytest
from optomind_research.runtime.upgrade3 import planning_material_triage as triage
from optomind_research.runtime.upgrade3 import planning_supplement as supplement
from optomind_research.runtime.upgrade3.module4 import runtime
from optomind_research.runtime.upgrade3.planning_material_search import PaperRecord, PlanningMaterialIndex

QUESTION = 'thermal fatigue coating'
BASE_BODY = (QUESTION + '. ') * 50 + 'BASELINE_ONLY: uncycled reference coupon.'

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('R2 independent boundary controls prohibit network')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)

def make_index(tmp_path, *, nominations=1, large=False):
    path = tmp_path / 'boundary.sqlite'
    with PlanningMaterialIndex(path) as index:
        index.upsert_paper(PaperRecord('base', 'BASE', 'Reference coupon', '2026', '', material_depth='fulltext'))
        index.add_segments('base', [{'segment_kind': 'document_block', 'ordinal': 0,
            'section_path': ['Control'], 'text': BASE_BODY}])
        for n in range(nominations):
            paper_id = f'nominated-{n}'
            index.upsert_paper(PaperRecord(paper_id, f'N{n}', f'Coupon {n}', '2026', '', material_depth='fulltext'))
            padding = 'Recorded apparatus conditions. ' * (45 if large else 0)
            rows = [
                {'segment_kind': 'document_block', 'ordinal': 0, 'section_path': [f'Experiment {n}'],
                 'text': (QUESTION + '. ') * 12 + f'BODY_{n}: coated coupons were cycled; field durability was not measured. ' + padding},
                {'segment_kind': 'card_key_finding', 'ordinal': 1, 'section_path': ['general understanding', 'key finding'],
                 'text': QUESTION + f' CARD_A_{n}_OLD: only the low-load coupon delayed crack onset. ' + padding},
                {'segment_kind': 'card_facet_contribution', 'ordinal': 2, 'section_path': ['review planning', 'facet contribution'],
                 'text': QUESTION + f' CARD_B_{n}: compare cyclic loading, not untested field service. ' + padding},
            ]
            index.add_segments(paper_id, rows)
        index.record_term_document_frequency()
        index.commit()
    return path

def boundary(tmp_path, monkeypatch, *, decision='external_research'):
    calls = []
    monkeypatch.setattr(runtime, 'QwenDirectClient', lambda **kwargs: object())
    def invoke(client, messages, **kwargs):
        calls.append(json.loads(messages[-1]['content']))
        return {'content': json.dumps({'decision': decision, 'answers_requested_question': False,
            'usable_content': 'Bounded laboratory observations do not establish field durability.',
            'still_missing': 'Field-service condition remains unmeasured.', 'read_focus': 'field durability'})}
    monkeypatch.setattr(runtime, 'invoke_client', invoke)
    judge = triage.QwenLocalTriageJudge(key_file=tmp_path / 'MUST_NOT_EXIST.key',
        budget_ledger_path=tmp_path / 'offline.sqlite', budget_limit_cny=1)
    return judge, calls

def request(nominations=1):
    return {'gap_id': 'R2-independent', 'question': QUESTION,
        'known_papers': [{'paper_id': f'nominated-{n}'} for n in range(nominations)]}

def run(path, judge, gap, **kwargs):
    return supplement.run_gap_local_triage(gap, index_path=path, judge=judge,
        read_local=False, top_papers=1, max_passages=1, **kwargs)

def source_text_chars(payload):
    return sum(len(row['text']) for row in payload['material_found']) + sum(
        len(row['opening']) + sum(map(len, row['sections'])) for row in payload['paper_context'])

def test_ordinary_first_payload_remains_exact(tmp_path, monkeypatch):
    path = make_index(tmp_path)
    judge, calls = boundary(tmp_path, monkeypatch)
    run(path, judge, request(0), source_handle_map={'BASE': 'base'})
    assert calls == [{
        'gap_id': 'R2-independent', 'question': QUESTION, 'intended_use': 'mechanism',
        'user_scope': '', 'success_criteria': [], 'reading_mode': 'initial_read',
        'material_found': [{'source_handle': 'BASE', 'type': 'direct_evidence', 'material_depth': 'fulltext',
                            'section': ['Control'], 'text': BASE_BODY}],
        'paper_context': [{'source_handle': 'BASE', 'opening': BASE_BODY, 'sections': ['Control']}],
        'local_search': {'found': True, 'not_matched_reason': '', 'papers_seen': 1},
    }]

def test_nominated_card_change_invalidates_only_changed_actual_input(tmp_path, monkeypatch):
    path = make_index(tmp_path)
    judge, calls = boundary(tmp_path, monkeypatch)
    kwargs = {'cache_dir': tmp_path / 'checkpoint'}
    first = run(path, judge, request(), **kwargs)
    replay = run(path, judge, request(), **kwargs)
    assert len(calls) == 1 and replay['model_calls'] == 0
    assert first['usable_content'] == replay['usable_content']
    with PlanningMaterialIndex(path) as index:
        rows = [dict(row) for kind in ('document_block', 'card_key_finding', 'card_facet_contribution')
                for row in index.segments_for('nominated-0', kind)]
        for row in rows:
            row['section_path'] = row['section_path'].split(' / ')
            row['text'] = row['text'].replace('CARD_A_0_OLD', 'CARD_A_0_NEW')
        index.bulk_replace_paper('nominated-0', rows)
        index.record_term_document_frequency()
        index.commit()
    changed = run(path, judge, request(), **kwargs)
    assert len(calls) == 2 and changed['model_calls'] == 1
    assert 'CARD_A_0_NEW' in '\n'.join(row['text'] for row in calls[-1]['material_found'])
    assert 'CARD_A_0_OLD' not in json.dumps(calls[-1])
    final = run(path, judge, request(), **kwargs)
    assert len(calls) == 2 and final['model_calls'] == 0
    assert len(list((tmp_path / 'checkpoint').glob('*/attempt-*/REQUEST.json'))) == 2

def test_nominated_cards_and_context_are_actual_payload_channels(tmp_path, monkeypatch):
    path = make_index(tmp_path)
    judge, calls = boundary(tmp_path, monkeypatch)
    result = run(path, judge, request())
    text = '\n'.join(row['text'] for row in calls[0]['material_found'] if row['source_handle'] == 'nominated-0')
    assert 'BODY_0' in text and 'CARD_A_0_OLD' in text and 'CARD_B_0' in text
    context = next(row for row in calls[0]['paper_context'] if row['source_handle'] == 'nominated-0')
    assert 'BODY_0' in context['opening'] and 'Experiment 0' in context['sections']
    assert any(row['paper_id'] == 'nominated-0' and row['reading_role'] == 'planning_summary'
               for row in result['writer_material']['sources'])

@pytest.mark.parametrize('nominations', [6, 12])
def test_explicit_cards_and_context_share_existing_increment(tmp_path, monkeypatch, nominations):
    path = make_index(tmp_path, nominations=nominations, large=True)
    judge, calls = boundary(tmp_path, monkeypatch)
    run(path, judge, request(0))
    result = run(path, judge, request(nominations))
    ordinary, explicit = calls
    assert explicit['material_found'][0] == ordinary['material_found'][0]
    assert source_text_chars(explicit) - source_text_chars(ordinary) <= triage.LOCAL_INCREMENT_CHARS
    # At the twelve-source maximum, intact long bodies can consume the
    # allowance before all card complements fit. Check real new content and
    # its source context, while the smaller case also guarantees A knowledge.
    delivered = {row['source_handle'] for row in explicit['material_found']
                 if any(f'BODY_{n}: coated coupons were cycled; field durability was not measured.' in row['text']
                        for n in range(nominations))}
    assert delivered
    if nominations == 6:
        assert any('CARD_A_' in row['text'] for row in explicit['material_found'])
    assert any(row['source_handle'] in delivered and 'BODY_' in row['opening']
               for row in explicit['paper_context'])
    assert any(row['source_handle'] in delivered and row['paper_id'].startswith('nominated-')
               for row in result['writer_material']['sources'])
    assert result['answers_requested_question'] is False

def test_nominations_beyond_old_context_quota_are_considered(tmp_path, monkeypatch):
    path = make_index(tmp_path, nominations=6)
    judge, calls = boundary(tmp_path, monkeypatch)
    result = run(path, judge, request(6))
    # Small packages fit comfortably; all six nominations get actual context,
    # rather than silently disappearing behind the old initial-ID-only [:4].
    contexts = {row['source_handle'] for row in calls[0]['paper_context']}
    assert {f'nominated-{n}' for n in range(6)}.issubset(contexts)
    assert all(result['local_reading']['explicit_reading_status'][f'nominated-{n}'].startswith('included')
               for n in range(6))
    assert len(contexts) <= 1 + triage.LOCAL_EXPANSION_PAPERS

def test_more_nominated_material_never_overrides_unanswered_verdict(tmp_path, monkeypatch):
    path = make_index(tmp_path)
    judge, calls = boundary(tmp_path, monkeypatch, decision='direct_use')
    result = run(path, judge, request())
    assert result['answers_requested_question'] is False
    assert result['decision'] == 'external_research'
    assert result['writer_material']['still_missing'] == 'Field-service condition remains unmeasured.'
    assert 'useful_context_does_not_answer_question' in result['reason']
