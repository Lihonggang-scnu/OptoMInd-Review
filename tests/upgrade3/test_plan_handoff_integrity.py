import json
from pathlib import Path

import pytest

from scripts.upgrade3 import fullbody_writer as body
from scripts.upgrade3 import outline_strengthening_pipeline as producer
from optomind_research.runtime.upgrade3.plan_handoff import file_hash, json_hash


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


@pytest.fixture
def selected(tmp_path):
    packets = {'CH01': {'chapter_id': 'CH01', 'source_materials': [],
                         'chapter_plan': {'units': [{'unit_id': 'U1', 'title': 'Original'}]}}}
    root = producer.export_complete_plan({'research_question': 'Frozen scope'}, packets, tmp_path,
                                         provenance={'status': 'accepted'})
    return tmp_path, root


def test_producer_consumer_records_all_selected_inputs(selected):
    output, root = selected
    actual, files = body.resolve_packet_root(output)
    assert actual == root
    assert {Path(row['path']).name for row in files} == {
        'CURRENT_PLAN.json', 'HANDOFF_MANIFEST.json', 'DETAILED_REVIEW_PLAN.json', 'CH01.json'}
    assert body.resolve_packet_root(root)[0] == root


@pytest.mark.parametrize('filename', ['writer_packets/CH01.json', 'DETAILED_REVIEW_PLAN.json', 'HANDOFF_MANIFEST.json'])
def test_first_consumption_rejects_changed_selected_bytes(selected, filename):
    output, root = selected
    path = root / filename
    value = json.loads(path.read_text())
    value['changed_after_selection'] = True
    write(path, value)
    with pytest.raises(ValueError, match='hash_mismatch'):
        body.resolve_packet_root(output)


def test_missing_explicit_manifest_never_becomes_legacy(selected):
    output, root = selected
    (root / 'HANDOFF_MANIFEST.json').unlink()
    with pytest.raises(ValueError, match='manifest_missing'):
        body.resolve_packet_root(output)


def test_legacy_manifest_still_checks_canonical_packet_hash(selected):
    output, root = selected
    manifest_path = root / 'HANDOFF_MANIFEST.json'
    manifest = json.loads(manifest_path.read_text())
    manifest.pop('schema_version'); manifest.pop('plan_sha256')
    write(manifest_path, manifest)
    pointer_path = output / 'CURRENT_PLAN.json'
    pointer = json.loads(pointer_path.read_text())
    pointer.pop('schema_version'); pointer.pop('manifest_sha256')
    write(pointer_path, pointer)
    packet_path = root / 'writer_packets/CH01.json'
    packet = json.loads(packet_path.read_text())
    packet_path.write_text(json.dumps(packet, indent=4), encoding='utf-8')
    assert body.resolve_packet_root(output)[0] == root
    packet['new_fact'] = 'changed'
    write(packet_path, packet)
    with pytest.raises(ValueError, match='packet_hash_mismatch'):
        body.resolve_packet_root(output)


def test_bound_scope_and_path_cannot_be_repointed(selected):
    output, root = selected
    plan_path = root / 'DETAILED_REVIEW_PLAN.json'
    plan = json.loads(plan_path.read_text())
    plan['writer_packets'][0]['json_path'] = '../elsewhere.json'
    write(plan_path, plan)
    manifest_path = root / 'HANDOFF_MANIFEST.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['plan_sha256'] = json_hash(plan)
    write(manifest_path, manifest)
    pointer_path = output / 'CURRENT_PLAN.json'
    pointer = json.loads(pointer_path.read_text())
    pointer['manifest_sha256'] = file_hash(manifest_path)
    write(pointer_path, pointer)
    with pytest.raises(ValueError, match='packet_path_mismatch'):
        body.resolve_packet_root(output)


def test_legacy_root_and_pointer_cycles(tmp_path):
    root = tmp_path / 'legacy'; root.mkdir()
    assert body.resolve_packet_root(root) == (root, [])
    pointer = root / 'CURRENT_PLAN.json'
    write(pointer, {'packet_root': '.'})
    with pytest.raises(ValueError, match='pointer_cycle'):
        body.resolve_packet_root(root)


def test_actual_arrangement_entry_rejects_before_model_construction(selected, tmp_path, monkeypatch):
    from scripts.upgrade3 import chapter_arrangement as arrangement
    output, root = selected
    packet_path = root / 'writer_packets/CH01.json'
    packet = json.loads(packet_path.read_text())
    packet['unselected_change'] = 'must not be written'
    write(packet_path, packet)
    monkeypatch.setattr(arrangement, 'RoundCappedLedger', lambda *a, **k: pytest.fail('Paid client boundary reached'))
    result = arrangement.main(['--packet-root', str(output), '--output-root', str(tmp_path / 'arranged'), '--run'])
    assert result == 2


@pytest.mark.parametrize('value', [None, '', 3])
def test_explicit_invalid_manifest_path_rejected(selected, value):
    output, _ = selected
    pointer_path = output / 'CURRENT_PLAN.json'
    pointer = json.loads(pointer_path.read_text())
    pointer['manifest'] = value
    write(pointer_path, pointer)
    with pytest.raises(ValueError, match='path_required'):
        body.resolve_packet_root(output)
