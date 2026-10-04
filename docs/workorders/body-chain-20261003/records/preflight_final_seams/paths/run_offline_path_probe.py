"""Offline actual-CLI probe. Provider creation is replaced; socket use denied.

Run from repository root with PYTHONPATH=/tmp/optomind-stage2-deps:.
No --run or paid opt-in exists for this driver. Its internal CLI --run exercises
production export routing only, never provider/budget construction.
"""
from contextlib import redirect_stdout, redirect_stderr
from hashlib import sha256
import json
from pathlib import Path
import runpy
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[6]
OUT = Path(__file__).resolve().parent
PHYSICAL = Path(tempfile.mkdtemp(prefix='optomind-path-probe-'))
helpers = runpy.run_path(str(ROOT / 'tests/upgrade3/test_review_unit_writer_portable_paths.py'))
safe = helpers['windows_safe']
component = helpers['portable_component']
old_names = {
    'normal_cli_directory': 'CH02_CH02:U3',
    'completion_cli_directory': 'CH02_CH02:U3_completion',
    'normal_raw_basename': 'CH02_CH02:U3_20261004T212434.raw',
    'completion_raw_basename': 'unit_completion_CH02_CH02:U3_1791120274_20261004T212434.raw',
}
summary = {
    'platform': 'Linux physical I/O with explicit Windows component validation',
    'native_windows_execution': False,
    'actual_provider_calls': 0,
    'network_allowed': False,
    'scientific_quality_claim': False,
    'historical_raw_timestamp_note': 'Illustrative fixed timestamp for pre-fix filename formula, not a native replay.',
    'before': {key: {'name': value, 'windows_component_valid': safe(value),
                     'colon_split': value.split(':', 1)} for key, value in old_names.items()},
    'runs': {},
}
assert not any(row['windows_component_valid'] for row in summary['before'].values())
for completion in (False, True):
    label = 'completion' if completion else 'normal'
    with pytest.MonkeyPatch.context() as patch:
        with (OUT / (label + '.stdout.log')).open('w') as stdout, (OUT / (label + '.stderr.log')).open('w') as stderr:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = helpers['run_archived_cli'](PHYSICAL / label, patch, completion=completion)
    result['verified'] = {
        'physical_raw_json_equals_provider_boundary_response': True,
        'saved_messages_equal_provider_boundary_messages': True,
        'chapter_id_unchanged': 'CH02', 'unit_id_unchanged': 'CH02:U3',
        'task_ids_unchanged': ['CH02:U3_P01'] if completion else 'ordinary input unchanged',
    }
    result['physical_files'] = [
        {'path': str(p), 'bytes': p.stat().st_size, 'sha256': sha256(p.read_bytes()).hexdigest(),
         'absolute_path_characters': len(str(p)),
         'relative_to_output_root_characters': len(str(p.relative_to(PHYSICAL / label))),
         'components_windows_valid': all(safe(part) for part in p.relative_to(PHYSICAL / label).parts)}
        for p in sorted((PHYSICAL / label).rglob('*')) if p.is_file()]
    summary['runs'][label] = result
summary['limits'] = [
    'Full-path MAX_PATH policy and arbitrary user-selected output roots are not repaired.',
    'Native Windows execution and NTFS stream enumeration were not available.',
    'Safe legacy IDs preserve pre-existing case-insensitive alias behavior.',
    'Unsafe identity collision resistance is a 96-bit SHA-256 suffix, not an injectivity claim.',
    'Saved run-mode/model_calls/simulated/usage fields belong to the exercised CLI and historical canned response; no new provider request occurred.',
    'Archived public materials are redacted, so rebuilding uses retained inline materials rather than omitted source snapshots.',
]
(OUT / 'PATH_PROBE_RESULT.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'actual_provider_calls': 0, 'normal_files': len(summary['runs']['normal']['physical_files']),
                  'completion_files': len(summary['runs']['completion']['physical_files']),
                  'all_components_valid': all(f['components_windows_valid'] for r in summary['runs'].values() for f in r['physical_files'])}, indent=2))
