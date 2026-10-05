"""R2 content counterexamples: real SQLite/adapters, synthetic prose, no network."""
import json
import os
import re
import socket
from pathlib import Path

import pytest

from optomind_research.runtime.upgrade3 import planning_material_search as ms
from optomind_research.runtime.upgrade3 import planning_material_triage as triage
from optomind_research.runtime.upgrade3 import planning_supplement as supplement
from test_body03_local_lookup_reading import capture_model, PARTIAL


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('R2 tests forbid sockets')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket, 'create_connection', deny)


def write_index(tmp_path, papers):
    path = tmp_path/'r2.sqlite'
    with ms.PlanningMaterialIndex(path) as index:
        for pid, rows in papers:
            index.upsert_paper(ms.PaperRecord(pid, pid.upper(), pid, '2026', '', material_depth='fulltext'))
            index.add_segments(pid, [{'ordinal': i, 'segment_kind': kind,
                                     'section_path': [section], 'text': text}
                                    for i, (kind, section, text) in enumerate(rows)])
        index.record_term_document_frequency()
        index.commit()
    return path


def text(payload):
    return '\n'.join(row['text'] for row in payload['material_found'])


def save_evidence(name, calls, result):
    if os.environ.get('R2_EVIDENCE_DIR'):
        out = Path(os.environ['R2_EVIDENCE_DIR'])
        out.mkdir(parents=True, exist_ok=True)
        (out/(name+'.json')).write_text(json.dumps({'requests': calls, 'writer_material': result['writer_material']}, indent=2))


def size(payload):
    return sum(len(row['text']) for row in payload['material_found']) + sum(
        len(row['opening']) + sum(map(len, row['sections'])) for row in payload['paper_context'])


def test_divergent_focus_preserves_original_material(tmp_path, monkeypatch):
    question = 'thermal fatigue coating'
    focus = 'population survey questionnaire adoption employment'
    papers = [(f'n{i}', [('document_block', 'Results', question+' '+question+' neighboring observation.')]) for i in range(7)]
    papers += [('target', [('document_block', 'Measured result', question+' ORIGINAL_BOUNDARY: porous coupons delayed cracking under cycling.')])]
    papers += [(f'f{i}', [('document_block', 'Survey', (focus+' ')*15+'Survey observation.')]) for i in range(20)]
    path = write_index(tmp_path, papers)
    with ms.PlanningMaterialIndex(path, readonly=True) as index:
        anchor = ms.search(index, question, top_papers=12)
        drift = ms.search(index, question+'\nRead specifically: '+focus, top_papers=12)
        assert 'target' in [h.paper_id for h in anchor.hits]
        assert 'target' not in [h.paper_id for h in drift.hits]
    def answer(payload):
        found = 'ORIGINAL_BOUNDARY' in text(payload)
        return {**PARTIAL, 'read_focus': focus, 'usable_content': 'ORIGINAL_BOUNDARY: porous coupons delayed cracking under cycling.' if found else 'Original boundary not supplied.'}
    judge, calls = capture_model(tmp_path, monkeypatch, answer)
    result = supplement.run_gap_local_triage({'gap_id': 'G', 'question': question}, index_path=path, judge=judge)
    save_evidence('divergent_focus', calls, result)
    assert len(calls) == 2
    assert 'ORIGINAL_BOUNDARY' not in text(calls[0])
    assert 'ORIGINAL_BOUNDARY' in text(calls[1])
    assert 'ORIGINAL_BOUNDARY' in result['writer_material']['usable_content']
    assert size(calls[1])-size(calls[0]) <= triage.LOCAL_INCREMENT_CHARS
    assert result['answers_requested_question'] is False


def package_fixture(tmp_path, discipline, cards=True, documents=True):
    query = 'thermal coating' if discipline == 'engineering' else 'retrieval learning'
    rows = []
    if documents:
        rows = [('document_block', 'Opening', 'OPENING_BOUNDARY: only the controlled setting was tested.')]
        rows += [('document_block', 'Spacer '+str(i), 'Unrelated procedural annotation.') for i in range(4)]
        rows += [('document_block', 'Specific result', query+' LEXICAL_RESULT: the controlled comparison changed the delayed outcome.')]
        rows += [('document_block', 'Spacer b'+str(i), 'Unrelated procedural annotation.') for i in range(4)]
        rows += [('document_block', 'Complementary result', query+' COMPLEMENTARY_BOUNDARY: transfer beyond the setting was not established.')]
        rows += [('document_block', 'Spacer c'+str(i), 'Unrelated procedural annotation.') for i in range(4)]
        rows += [('document_block', 'General discussion', (query+' ')*4+'GENERAL_ONLY: no comparison is provided. '+('Broad introductory discussion. '*200))]
    if cards:
        rows += [('card_key_finding', 'A key finding', 'A_KNOWLEDGE: the manipulated interlayer or interval altered the outcome under controlled conditions.'),
                 ('card_facet_contribution', 'B contribution', 'B_KNOWLEDGE: preserve the delayed observation and do not infer general transfer.')]
    if discipline == 'education':
        substitutions = {
            'only the controlled setting was tested.': 'only novice vocabulary learners were tested.',
            'the controlled comparison changed the delayed outcome.': 'spaced quizzes improved delayed vocabulary recall over restudy.',
            'transfer beyond the setting was not established.': 'the recall benefit did not extend to unseen grammar problems.',
            'the manipulated interlayer or interval altered the outcome under controlled conditions.': 'retrieval spacing improved retention in the classroom cohort.',
            'preserve the delayed observation and do not infer general transfer.': 'retain the delayed recall versus grammar transfer distinction.',
        }
        revised = []
        for kind, section, body in rows:
            for before, after in substitutions.items():
                body = body.replace(before, after)
            revised.append((kind, section, body))
        rows = revised
    path = write_index(tmp_path, [('neighbor', [('document_block', 'Neighbor', (query+' ')*100)]), ('target', rows)])
    return path, query


@pytest.mark.parametrize('discipline', ['engineering', 'education'])
def test_nominated_package_delivers_complementary_content(tmp_path, monkeypatch, discipline):
    path, question = package_fixture(tmp_path, discipline)
    with ms.PlanningMaterialIndex(path, readonly=True) as index:
        normal = ms.search(index, question, paper_ids=['target'], top_papers=1, passages_per_paper=3, context_chars=0, kinds=['document_block'])
        assert len(normal.hits) == 1
        assert 'GENERAL_ONLY' in normal.hits[0].text
        raw = index._conn.execute('SELECT text FROM segment_fts WHERE segment_fts MATCH ? AND paper_id=? ORDER BY bm25(segment_fts)', (ms.fts_query(ms.expand_query(question)), 'target')).fetchone()
        assert 'LEXICAL_RESULT' in raw[0] or 'COMPLEMENTARY_BOUNDARY' in raw[0]
    def answer(payload):
        supplied = [match.group() for marker in ('LEXICAL_RESULT', 'COMPLEMENTARY_BOUNDARY', 'A_KNOWLEDGE', 'B_KNOWLEDGE')
                    for match in [re.search(marker+r'[^.]+\.', text(payload))] if match]
        return {**PARTIAL, 'usable_content': '\n'.join(supplied)}
    judge, calls = capture_model(tmp_path, monkeypatch, answer)
    result = supplement.run_gap_local_triage({'gap_id': 'G', 'question': question, 'known_papers': [{'paper_id': 'target'}]}, index_path=path, judge=judge, top_papers=1, max_passages=1, read_local=False)
    save_evidence('package_'+discipline, calls, result)
    for marker in ('LEXICAL_RESULT', 'COMPLEMENTARY_BOUNDARY', 'A_KNOWLEDGE', 'B_KNOWLEDGE'):
        assert marker in text(calls[0]), marker
        assert marker in result['writer_material']['usable_content'], marker
    observation = ('the controlled comparison changed the delayed outcome.' if discipline == 'engineering'
                   else 'spaced quizzes improved delayed vocabulary recall over restudy.')
    boundary = ('transfer beyond the setting was not established.' if discipline == 'engineering'
                else 'the recall benefit did not extend to unseen grammar problems.')
    for fact in (observation, boundary):
        assert fact in text(calls[0])
        assert fact in result['writer_material']['usable_content']
    assert any(row['source_handle']=='target' and 'OPENING_BOUNDARY' in row['opening'] for row in calls[0]['paper_context'])
    assert result['answers_requested_question'] is False



@pytest.mark.parametrize('cards,documents', [(False, True), (True, False)])
def test_package_degrades_to_existing_channels(tmp_path, monkeypatch, cards, documents):
    path, question = package_fixture(tmp_path, 'education', cards=cards, documents=documents)
    judge, calls = capture_model(tmp_path, monkeypatch, lambda payload: {
        **PARTIAL, 'usable_content': text(payload)})
    result = supplement.run_gap_local_triage({'gap_id': 'G', 'question': question,
        'known_papers': [{'paper_id': 'target'}]}, index_path=path, judge=judge,
        top_papers=1, max_passages=1, read_local=False)
    expected = ('spaced quizzes improved delayed vocabulary recall over restudy.' if documents
                else 'retain the delayed recall versus grammar transfer distinction.')
    assert expected in text(calls[0])
    assert expected in result['writer_material']['usable_content']
    assert result['answers_requested_question'] is False
    if not documents:
        assert 'retrieval spacing improved retention in the classroom cohort.' in text(calls[0])
    if not cards:
        assert 'A_KNOWLEDGE' not in text(calls[0])


def test_nominated_context_filters_legacy_reference_opening(tmp_path, monkeypatch):
    path = write_index(tmp_path, [
        ('neighbor', [('document_block', 'Neighbor', 'thermal coating '*100)]),
        ('target', [('document_block', 'References', 'REFERENCE_ONLY: thermal coating bibliography. '*30),
                    ('document_block', 'Study conditions', 'SUBSTANTIVE_OPENING: thermal coating was tested only on cycled coupons.')])])
    # Existing indexes may predate filtering; ensure this exercises retained
    # legacy publication metadata rather than assuming the build rejected it.
    with ms.PlanningMaterialIndex(path, readonly=True) as index:
        assert any('REFERENCE_ONLY' in row['text'] for row in index.segments_for('target', 'document_block'))
    judge, calls = capture_model(tmp_path, monkeypatch, PARTIAL)
    supplement.run_gap_local_triage({'gap_id': 'G', 'question': 'thermal coating',
        'known_papers': [{'paper_id': 'target'}]}, index_path=path, judge=judge,
        top_papers=1, max_passages=1, read_local=False)
    contexts = [row for row in calls[0]['paper_context'] if row['source_handle'] == 'target']
    assert contexts and 'SUBSTANTIVE_OPENING' in contexts[0]['opening']
    assert 'REFERENCE_ONLY' not in json.dumps(contexts)
    assert 'References' not in contexts[0]['sections']
