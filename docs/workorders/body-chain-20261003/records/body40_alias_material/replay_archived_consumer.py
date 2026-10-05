"""Offline archive consumer replay; persists identity metadata/hashes only."""
from pathlib import Path
from unittest.mock import patch
import hashlib
import json
import subprocess
import sys
import types

from optomind_research.runtime.upgrade3.review_unit_writer import build_unit_view, _known_unit_handles, unit_messages

ARCHIVE = Path('docs/acceptance/body40-20261005')
ARRANGEMENT = ARCHIVE / 'body/body_assembly_final/arrangements/Ch6/CHAPTER_ARRANGEMENT.json'
VIEW_INPUT = ARCHIVE / 'body/arrangement_repaired/Ch6/ARRANGEMENT_INPUT.json'
REPAIRED_COPY = ARCHIVE / 'body/arrangement_repaired/Ch6/CHAPTER_ARRANGEMENT.json'
OUTPUT = Path(__file__).with_name('ACTUAL_CONSUMER_REPLAY.json')
SCIENCE_KEYS = ['study_summary_A', 'review_planning_B', 'supplement_material', 'supplement_materials',
                'deep_read_material', 'deep_read_materials', 'local_passages', 'local_passages_variants',
                'tool_supplement_materials']


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def main():
    arrangement = json.loads(ARRANGEMENT.read_text())
    original_is_file = Path.is_file
    foreign_paths = {
        str((entry.get("locator") or {}).get(key) or "")
        for entry in arrangement.get("source_catalog", {}).values()
        for key in ("card_path", "writer_packet")
        if str((entry.get("locator") or {}).get(key) or "")[1:3] == ':\\'
    }
    baseline_commit = 'e75c66c1ffd018a93636960caa7c51e74911c2af'
    baseline_source = subprocess.check_output([
        'git', 'show', baseline_commit + ':optomind_research/runtime/upgrade3/review_unit_writer.py'], text=True)
    baseline = types.ModuleType('optomind_research.runtime.upgrade3._body40_baseline_writer')
    baseline.__package__ = 'optomind_research.runtime.upgrade3'
    baseline.__file__ = str(Path('optomind_research/runtime/upgrade3/review_unit_writer.py').resolve())
    sys.modules[baseline.__name__] = baseline
    exec(compile(baseline_source, '<git-baseline-writer>', 'exec'), baseline.__dict__)

    def portable_is_file(path):
        # These Windows-located cards are not available in the Linux archive.
        # Avoid Linux ENAMETOOLONG for a literal Windows path, without reading,
        # translating, supplying or changing any archived material.
        return False if str(path) in foreign_paths else original_is_file(path)

    records = []
    for unit_id in ['Ch6_U1', 'Ch6_U2']:
        with patch.object(Path, 'is_file', portable_is_file):
            baseline_view = baseline.build_unit_view(ARRANGEMENT, unit_id, view_path=VIEW_INPUT, max_material_chars_per_source=0)
            view = build_unit_view(ARRANGEMENT, unit_id, view_path=VIEW_INPUT, max_material_chars_per_source=0)
        old_messages = baseline.unit_messages(baseline_view, planning_revision=True)
        new_messages = unit_messages(view, planning_revision=True)
        old_payload = json.loads(old_messages[-1]['content'])
        new_payload = json.loads(new_messages[-1]['content'])
        old_message_source = next(item for item in old_payload['sources'] if item['source_handle'] == 'P0049')
        new_message_source = next(item for item in new_payload['sources'] if item['source_handle'] == 'P0049')
        old_message_science = {key: old_message_source[key] for key in SCIENCE_KEYS if key in old_message_source}
        new_message_science = {key: new_message_source[key] for key in SCIENCE_KEYS if key in new_message_source}
        source = next(item for item in view.materials if item['source_handle'] == 'P0049')
        unit = next(item for item in arrangement['units'] if item['unit_id'] == unit_id)
        previous_path = ARCHIVE / 'writer/live/Ch6' / ('Ch6_' + unit_id) / 'UNIT_INPUT.json'
        previous = json.loads(previous_path.read_text())
        prior_canonical = next(item for item in previous['materials'] if item['source_handle'] == 'P0049')
        before = {key: prior_canonical[key] for key in SCIENCE_KEYS if key in prior_canonical}
        after = {key: source[key] for key in SCIENCE_KEYS if key in source}
        records.append({
            'unit_id': unit_id, 'arrangement_path': str(ARRANGEMENT),
            'view_input_path': str(VIEW_INPUT),
            'view_input_sha256': hashlib.sha256(VIEW_INPUT.read_bytes()).hexdigest(),
            'baseline_commit': baseline_commit,
            'baseline_messages_sha256': digest(old_messages),
            'current_messages_sha256': digest(new_messages),
            'system_prompt_unchanged': old_messages[0] == new_messages[0],
            'message_canonical_science_before_sha256': digest(old_message_science),
            'message_canonical_science_after_sha256': digest(new_message_science),
            'message_canonical_science_unchanged': old_message_science == new_message_science,
            'message_canonical_material_count': sum(item['source_handle'] == 'P0049' for item in new_payload['sources']),
            'message_alias_material_count': sum(item['source_handle'] == 'P0605' for item in new_payload['sources']),
            'message_alias_missing_before': any(item['source_handle'] == 'P0605' and item.get('missing_material') for item in old_payload['sources']),
            'message_alias_missing_after': any(item['source_handle'] == 'P0605' and item.get('missing_material') for item in new_payload['sources']),
            'message_tasks_unchanged': all(old_payload[k] == new_payload[k] for k in ('paragraph_tasks', 'table_tasks')),
            'baseline_alias_missing_same_portability_adapter': any(
                item['source_handle'] == 'P0605' and item.get('missing_material')
                for item in baseline_view.materials),
            'arrangement_sha256': hashlib.sha256(ARRANGEMENT.read_bytes()).hexdigest(),
            'prior_input_path': str(previous_path),
            'prior_input_sha256': hashlib.sha256(previous_path.read_bytes()).hexdigest(),
            'prior_alias_missing': any(item['source_handle'] == 'P0605' and item.get('missing_material')
                                       for item in previous['materials']),
            'new_alias_missing': any(item['source_handle'] == 'P0605' and item.get('missing_material')
                                     for item in view.materials),
            'canonical_material_count': sum(item['source_handle'] == 'P0049' for item in view.materials),
            'alias_material_count': sum(item['source_handle'] == 'P0605' for item in view.materials),
            'known_alias_and_canonical': {'P0049', 'P0605'}.issubset(_known_unit_handles(view)),
            'original_source_uses_preserved': all(
                [task.get('source_uses') for task in unit.get(kind, [])] ==
                [task.get('source_uses') for task in getattr(view, kind)]
                for kind in ['paragraph_tasks', 'table_tasks']),
            'original_task_ids_preserved': all(
                [task.get(key) for task in unit.get(kind, [])] ==
                [task.get(key) for task in getattr(view, kind)]
                for kind, key in [('paragraph_tasks', 'paragraph_id'), ('table_tasks', 'table_id')]),
            'material_science_before_sha256': digest(before),
            'material_science_after_sha256': digest(after), 'material_science_unchanged': before == after,
            'new_missing_material_handles': [item['source_handle'] for item in view.materials
                                             if item.get('missing_material')],
        })
    OUTPUT.write_text(json.dumps({
        'source': 'Existing public acceptance archive; native snapshot-selection metadata informs path selection only; native raw content not replayed',
        'snapshot_selection': {
            'arrangement_path': str(ARRANGEMENT),
            'arrangement_sha256': hashlib.sha256(ARRANGEMENT.read_bytes()).hexdigest(),
            'view_input_path': str(VIEW_INPUT),
            'view_input_sha256': hashlib.sha256(VIEW_INPUT.read_bytes()).hexdigest(),
            'public_repaired_copy_path': str(REPAIRED_COPY),
            'public_repaired_copy_sha256': hashlib.sha256(REPAIRED_COPY.read_bytes()).hexdigest(),
            'public_arrangement_copies_equal': ARRANGEMENT.read_bytes() == REPAIRED_COPY.read_bytes(),
            'limitation': 'Public archive copies are compared directly here; original-local snapshot equality is not assumed',
        },
        'runtime_mode': 'Offline read-only build_unit_view and unit_messages; no model call',
        'max_material_chars_per_source': 0,
        'planning_revision': True,
        'parameter_basis': 'Historical BODY40 unbounded source material and planning-revision message mode; generation parameters not invoked',
        'portability_adapter': 'Exact foreign Windows card/packet paths in this archived catalog treated as unavailable on Linux for baseline and current consumer',
        'records': records,
    }, indent=2) + '\n')
    print(json.dumps(records, indent=2))


if __name__ == '__main__':
    main()
