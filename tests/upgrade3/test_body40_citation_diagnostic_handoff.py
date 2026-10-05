"""Offline real-file writer CLI -> UNIT_RESULT -> assembly diagnostic replay."""
import json
import socket
from pathlib import Path

import pytest

from scripts.upgrade3 import full_review_draft as assembly
from scripts.upgrade3 import review_unit_writer as cli

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / 'docs/acceptance/body40-20261005'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('Network forbidden in BODY40 diagnostic replay')
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(socket.socket, 'connect', denied)
    # Avoid tokenizer initialization reaching a model registry as well.
    monkeypatch.setattr(cli, '_default_qwen_token_counter', lambda: None)


def assemble(tmp_path, arrangement, result_path):
    result = read(result_path)
    unit_id, chapter_id = result['unit_id'], result['chapter_id']
    arrangement = dict(arrangement)
    arrangement['units'] = [u for u in arrangement['units'] if u['unit_id'] == unit_id]
    arrangement['issues'] = []
    arrangement['status'] = 'complete'
    arrangement_path = put(tmp_path / 'arrangement.json', arrangement)
    manifest = put(tmp_path / 'manifest.json', {
        'review_title': 'Offline diagnostic replay', 'planning_status': 'complete',
        'arrangement_status': 'complete',
        'chapters': [{'chapter_id': chapter_id, 'arrangement_path': str(arrangement_path)}],
    })
    batch = tmp_path / 'batch'
    put(batch / 'BATCH_JOBS.json', [{'chapter_id': chapter_id, 'unit_id': unit_id,
        'arrangement': str(arrangement_path), 'reused_result': str(result_path),
        'output': str(result_path.parent)}])
    output = tmp_path / 'assembled'
    assert assembly.main(['--manifest', str(manifest), '--batch-root', str(batch),
                          '--output-root', str(output)]) == 0
    return read(output / 'ASSEMBLY_SUMMARY.json'), output


def controlled_input(tmp_path, domain='astronomy'):
    titles = {'astronomy': 'Stellar Oscillations in Red Giants',
              'materials': 'Interfacial Transport in Ceramic Membranes'}
    arrangement = {'chapter_id': 'Ch1', 'title': domain, 'source_catalog': {
        'P0011': {'source_handle': 'P0011', 'paper_id': domain + '-stable-id',
                  'title': titles[domain], 'study_summary_A': {'finding': 'Controlled evidence.'}}},
        'units': [{'unit_id': 'U1', 'focus': domain, 'paragraph_tasks': [
            {'paragraph_id': 'P1', 'source_uses': [{'source_handle': 'P0011'}]}],
            'table_tasks': []}]}
    path = put(tmp_path / 'CHAPTER_ARRANGEMENT.json', arrangement)
    put(tmp_path / 'ARRANGEMENT_INPUT.json', {'chapter_id': 'Ch1', 'title': domain})
    return path, arrangement, titles[domain]


def writer_cli(tmp_path, monkeypatch, arrangement_path, unit, response):
    calls = []
    def replay(messages, **kwargs):
        calls.append(messages)
        return response
    # Production CLI, replacing only the paid provider boundary with stored bytes.
    monkeypatch.setattr(cli, '_real_client', lambda *args, **kwargs: replay)
    output = tmp_path / 'writer'
    assert cli.main(['--arrangement', str(arrangement_path), '--unit', unit,
                     '--output-root', str(output), '--run', '--planning-revision',
                     '--max-material-chars-per-source', '0']) == 0
    assert len(calls) == 1
    report = read(output / 'UNIT_WRITING_RUN.json')
    return Path(report['units'][0]['result_path']), report


def test_archived_ch6_unresolved_diagnostics_reach_assembly(tmp_path):
    source = ARCHIVE / 'writer/live/Ch6/Ch6_Ch6_U4/UNIT_RESULT.json'
    before = source.read_bytes()
    result = read(source)
    assert len(result['citation_problems']) == 11
    result['body_path'] = ''  # Archived Windows locator is not a production cache.
    result_path = put(tmp_path / 'replay/UNIT_RESULT.json', result)
    arrangement = read(ARCHIVE / 'body/arrangement_repaired/Ch6/CHAPTER_ARRANGEMENT.json')
    summary, output = assemble(tmp_path, arrangement, result_path)
    assert summary['status'] == 'complete'
    assert summary['problems_resolved'] is False
    assert summary['unit_rows'][0]['citation_problems'] == result['citation_problems']
    assert summary['pending_problems'][0]['citation_problems'] == result['citation_problems']
    assert result['body_markdown'] in (output / 'REVIEW_DRAFT_HANDLES.md').read_text()
    assert 'numeric_citation_unresolved' in (output / 'RUN_REPORT.md').read_text()
    assert source.read_bytes() == before


@pytest.mark.parametrize('domain', ['astronomy', 'materials'])
def test_cli_retains_successful_title_repairs_without_blocking(tmp_path, monkeypatch, domain):
    path, arrangement, title = controlled_input(tmp_path, domain)
    result_path, report = writer_cli(tmp_path, monkeypatch, path, 'U1', {
        'content': json.dumps({'body_markdown': 'Controlled finding [1].\n\n[1] ' + title}),
        'complete': True, 'finish_reason': 'stop'})
    result = read(result_path)
    assert result['numeric_citation_repairs']
    assert result['bibliography_title_citation_map'] == {'1': 'P0011'}
    assert result['citation_number_map_origin'] == 'generated_local_aliases'
    assert report['units'][0]['numeric_citation_repairs'] == result['numeric_citation_repairs']
    summary, output = assemble(tmp_path / 'assembly', arrangement, result_path)
    assert summary['status'] == 'complete'
    assert summary['problems_resolved'] is True
    assert summary['unit_rows'][0]['numeric_citation_repairs'] == result['numeric_citation_repairs']
    assert 'Controlled finding [P0011].' in (output / 'REVIEW_DRAFT_HANDLES.md').read_text()


def relocated_archive_input(tmp_path, chapter_id, original):
    archive_arrangement = ARCHIVE / 'body/arrangement_repaired' / chapter_id / 'CHAPTER_ARRANGEMENT.json'
    arrangement = read(archive_arrangement)
    # Historical Windows locators are evidence, never portable production cache.
    # Replay the archived material payload in a separate copy; no retrieval.
    for source in arrangement['source_catalog'].values():
        source.pop('locator', None)
    saved_input = read(original / 'UNIT_INPUT.json')
    for material in saved_input['materials']:
        handle = material['source_handle']
        if handle in arrangement['source_catalog']:
            arrangement['source_catalog'][handle].update({
                key: value for key, value in material.items() if key != 'locator'})
    path = put(tmp_path / 'input/CHAPTER_ARRANGEMENT.json', arrangement)
    put(path.with_name('ARRANGEMENT_INPUT.json'), read(archive_arrangement.with_name('ARRANGEMENT_INPUT.json')))
    return path


def test_real_raw_ch6_cli_replay_preserves_numeric_uncertainty(tmp_path, monkeypatch):
    original = ARCHIVE / 'writer/live/Ch6/Ch6_Ch6_U4'
    response_file = next((original / 'raw_responses').glob('*.raw'))
    before = response_file.read_bytes()
    path = relocated_archive_input(tmp_path, 'Ch6', original)
    result_path, _ = writer_cli(tmp_path, monkeypatch, path, 'Ch6_U4', read(response_file))
    result = read(result_path)
    assert '[11]' in result['body_markdown']
    assert '[P0011]' not in result['body_markdown']
    assert '[11]' in result['unresolved_numeric_citations']
    assert result['numeric_citation_repairs'] == []
    assert result['citation_number_map_origin'] == 'generated_local_aliases'
    summary, output = assemble(tmp_path / 'assembly', read(path), result_path)
    assert summary['status'] == 'complete'
    assert summary['problems_resolved'] is False
    assert result['body_markdown'] in (output / 'REVIEW_DRAFT_HANDLES.md').read_text()
    assert response_file.read_bytes() == before


@pytest.mark.parametrize('field,value', [
    ('unresolved_numeric_citations', ['[11]']),
    ('non_source_identifier_citations', ['[Q01]']),
    ('citation_mapping_diagnostics', [{'code': 'numeric_citation_mapping_ambiguous', 'number': '1'}]),
])
def test_legacy_missing_problem_list_still_pending(tmp_path, field, value):
    _, arrangement, _ = controlled_input(tmp_path)
    result_path = put(tmp_path / 'UNIT_RESULT.json', {
        'chapter_id': 'Ch1', 'unit_id': 'U1', 'body_markdown': 'Useful text [11] [Q01].',
        'complete': True, 'issues': [], field: value})
    summary, output = assemble(tmp_path / 'assembly', arrangement, result_path)
    assert summary['problems_resolved'] is False
    assert summary['pending_problems'][0]['citation_problems']
    assert 'Useful text [11] [Q01].' in (output / 'REVIEW_DRAFT_HANDLES.md').read_text()


def test_explicit_informational_citation_diagnostics_are_nonblocking(tmp_path):
    _, arrangement, _ = controlled_input(tmp_path)
    information = {'code': 'numeric_citation_repaired', 'severity': 'info', 'citation': '[1]'}
    result_path = put(tmp_path / 'UNIT_RESULT.json', {'chapter_id': 'Ch1', 'unit_id': 'U1',
        'body_markdown': 'Useful text [P0011].', 'complete': True, 'issues': [],
        'citation_problems': [information], 'citation_mapping_diagnostics': [information]})
    summary, _ = assemble(tmp_path / 'assembly', arrangement, result_path)
    assert summary['problems_resolved'] is True
    assert summary['unit_rows'][0]['citation_problems'] == [information]


def test_offline_fake_cli_script_entry_persists_repair_diagnostics(tmp_path):
    import subprocess
    import sys
    path, _, title = controlled_input(tmp_path, 'materials')
    reply = put(tmp_path / 'reply.json', {'content': json.dumps({
        'body_markdown': 'Controlled finding [1].\n\n[1] ' + title}),
        'complete': True, 'finish_reason': 'stop'})
    output = tmp_path / 'subprocess'
    guard = ('import runpy, socket, sys; '
             'deny=lambda *a, **k: (_ for _ in ()).throw(AssertionError("Network forbidden")); '
             'socket.create_connection=deny; socket.socket.connect=deny; '
             'sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0], run_name="__main__")')
    completed = subprocess.run([sys.executable, '-c', guard,
        str(ROOT / 'scripts/upgrade3/review_unit_writer.py'),
        '--arrangement', str(path), '--unit', 'U1', '--output-root', str(output),
        '--planning-revision', '--fake-client', str(reply)], cwd=ROOT,
        capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    report = read(output / 'UNIT_WRITING_RUN.json')
    assert report['model_calls'] == 0
    result = read(Path(report['units'][0]['result_path']))
    assert result['simulated'] is True
    assert result['numeric_citation_repairs']
    assert result['bibliography_title_citation_map'] == {'1': 'P0011'}


def test_real_raw_ch7_known_question_identity_reaches_pending(tmp_path, monkeypatch):
    original = ARCHIVE / 'writer/live/Ch7/Ch7_Ch7_U02'
    response_file = next((original / 'raw_responses').glob('*.raw'))
    before = response_file.read_bytes()
    path = relocated_archive_input(tmp_path, 'Ch7', original)
    result_path, _ = writer_cli(tmp_path, monkeypatch, path, 'Ch7_U02', read(response_file))
    result = read(result_path)
    assert 'Q01' in result['known_tool_identifiers']
    assert result['body_markdown'].count('[Q01]') == 5
    assert any(p['code'] == 'non_source_identifier_citation'
               for p in result['citation_problems'])
    summary, output = assemble(tmp_path / 'assembly', read(path), result_path)
    assert summary['status'] == 'complete'
    assert summary['problems_resolved'] is False
    assembled = (output / 'REVIEW_DRAFT_HANDLES.md').read_text()
    assert assembled.count('[Q01]') == 5
    for paragraph in result['body_markdown'].split('\n\n'):
        if paragraph.strip() and not paragraph.lstrip().startswith('#'):
            assert paragraph in assembled
    assert 'non_source_identifier_citation' in (output / 'RUN_REPORT.md').read_text()
    assert response_file.read_bytes() == before
