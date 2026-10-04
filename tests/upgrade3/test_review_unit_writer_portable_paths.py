"""Linux file I/O plus explicit Windows component rules; not native Windows."""
import json
from pathlib import Path, PureWindowsPath
import re
import socket

import pytest

from optomind_research.runtime.upgrade3.portable_paths import portable_component
from scripts.upgrade3 import review_unit_writer as cli

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / 'docs/acceptance/preflight-materials-local-20261004'


def windows_safe(name):
    return (bool(name) and name not in {'.', '..'}
            and not re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', name)
            and not name.endswith(('.', ' '))
            and not PureWindowsPath(name).is_reserved()
            and len(name.encode('utf-16-le')) // 2 <= 255)


@pytest.mark.parametrize('value', [
    '', '.', '..', 'CH02:U3', 'CH02/U3', 'CH02\\U3', '../outside',
    'C:\\temp', 'name:stream', 'name.', 'name ', 'CON', 'con.txt',
    'NUL', 'AUX.json', 'PRN', 'COM1', 'LPT9.log', 'COM¹', 'LPT².txt',
    'CONIN$', 'CONOUT$', 'x\0y', 'x\ny', 'x\x7fy', 'a'*1000,
    '汉'*200, 'a?b', 'a*b', 'a|b', '<a>', '"a"',
])
def test_unsafe_ids_are_portable_and_deterministic(value):
    result = portable_component(value)
    assert windows_safe(result)
    assert result == portable_component(value)
    assert result != value


@pytest.mark.parametrize('value', ['CH02_U3', 'CH02_CH02_U3_completion',
                                       'chapter-unit_01.raw', '单元一', 'CONtrast'])
def test_safe_legacy_names_preserved(value):
    assert portable_component(value) == value


def test_lossy_slug_and_encoded_namespace_do_not_collide():
    values = ['CH02:U3', 'CH02/U3', 'CH02\\U3', 'CH02_U3', 'CH02?U3']
    encoded = [portable_component(v) for v in values]
    assert len(set(encoded)) == len(values)
    assert portable_component(encoded[0]) != encoded[0]
    assert portable_component(encoded[0].upper()).casefold() != encoded[0].casefold()
    assert portable_component('CH02:u3').casefold() != encoded[0].casefold()


def deny_network(*args, **kwargs):
    raise AssertionError('Network forbidden in offline path test')


def run_archived_cli(output_root, monkeypatch, *, completion=False, mode='run'):
    """Actual build/view/messages/CLI/export; replace only provider boundary."""
    monkeypatch.setattr(socket, 'create_connection', deny_network)
    monkeypatch.setattr(socket.socket, 'connect', deny_network)
    calls = []
    response = json.loads((ARCHIVE / 'live_writer_qwen37_single_v2/CH02_U3/RAW_RESPONSE.json').read_text())
    if completion:
        response = {'content': json.dumps({'body_markdown': 'Offline path probe [P0602].',
                    'status': 'appended', 'covered_task_ids': ['CH02:U3_P01'], 'issues': []}),
                    'complete': True, 'finish_reason': 'stop'}

    def client(messages, **kwargs):
        calls.append({'messages': messages, 'kwargs': kwargs})
        return response

    monkeypatch.setattr(cli, '_real_client', lambda *args, **kwargs: client)
    results = ARCHIVE / 'real_checks/results'
    argv = ['--arrangement', str(results / 'ARRANGEMENT_FROM_REAL_PACKET.json'),
            '--view', str(results / 'ARRANGEMENT_INPUT_FROM_REAL_PACKET.json'),
            '--unit', 'CH02:U3', '--output-root', str(output_root)]
    if completion:
        argv += ['--existing-body', str(ARCHIVE / 'live_writer_qwen37_single_v2/CH02_U3/UNIT_BODY.md'),
                 '--complete-task', 'CH02:U3_P01']
    if mode == 'run':
        argv += ['--run']
    elif mode == 'fake':
        reply_file = output_root.parent / (output_root.name + '_reply.json')
        reply_file.write_text(json.dumps(response), encoding='utf-8')
        argv += ['--fake-client', str(reply_file)]
    assert cli.main(argv) == 0
    report_name = 'COMPLETION_RUN.json' if completion else 'UNIT_WRITING_RUN.json'
    report = json.loads((output_root / report_name).read_text())
    entry = report['units'][0]
    assert entry['chapter_id'] == 'CH02' and entry['unit_id'] == 'CH02:U3'
    if completion:
        directory = Path(entry['output_dir'])
        input_file = directory / 'COMPLETION_INPUT.json'
        messages_file = directory / 'COMPLETION_MESSAGES.json'
    else:
        input_file = Path(entry['input_path'])
        directory = input_file.parent
        messages_file = Path(entry['messages_path'])
    assert directory.parent == output_root or directory.parent == output_root / '_simulated'
    data = json.loads(input_file.read_text())
    assert data['unit_id'] == 'CH02:U3'
    messages = json.loads(messages_file.read_text())
    assert json.loads(messages[-1]['content'])['unit_id'] == 'CH02:U3'
    if completion:
        assert entry['task_ids'] == ['CH02:U3_P01']
        payload = data['payload']
        assert payload['requested_task_ids'] == ['CH02:U3_P01']
    if mode == 'run':
        assert calls and calls[0]['messages'] == messages
        raw_files = list((directory / 'raw_responses').glob('*.raw'))
        assert len(raw_files) == 1
        assert json.loads(raw_files[0].read_text()) == response
        result = json.loads((directory / ('COMPLETION_RESULT.json' if completion else 'UNIT_RESULT.json')).read_text())
        assert Path(result['raw_response']).is_file()
        assert result['unit_id'] == 'CH02:U3'
    for file in output_root.rglob('*'):
        assert windows_safe(file.name), file
    return {'argv': argv, 'report': report, 'provider_boundary_calls': len(calls),
            'actual_provider_calls': 0, 'native_windows_execution': False,
            'paths': [str(p.relative_to(output_root)) for p in sorted(output_root.rglob('*')) if p.is_file()]}


@pytest.mark.parametrize('completion', [False, True])
@pytest.mark.parametrize('mode', ['preview', 'fake', 'run'])
def test_archived_identity_through_actual_cli(tmp_path, monkeypatch, completion, mode):
    run_archived_cli(tmp_path / 'outputs', monkeypatch, completion=completion, mode=mode)
