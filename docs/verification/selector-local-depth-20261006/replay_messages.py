"""Offline production-message comparison; synthetic responses, zero model calls."""
from __future__ import annotations
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests' / 'upgrade3')]
from optomind_research.runtime.upgrade3 import outline_selection as current
from optomind_research.runtime.upgrade3 import outline_on_demand as demand
from optomind_research.runtime.upgrade3 import outline_strengthening as owner
from test_outline_selection import _payload

BASE = 'b56cba83436ebf76208807ab31bb569afb611fb6'
OUT = Path(__file__).resolve().parent

def dump(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def main():
    baseline_source = subprocess.check_output(['git', 'show', BASE + ':optomind_research/runtime/upgrade3/outline_selection.py'], cwd=ROOT, text=True)
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / 'baseline.py'
        path.write_text(baseline_source, encoding='utf-8')
        spec = importlib.util.spec_from_file_location('optomind_research.runtime.upgrade3._selector_baseline_replay', path)
        baseline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(baseline)
    packets = {'CH-A': _payload('CH-A', tail='Optical response comparison'), 'CH-B': _payload('CH-B', tail='Engineering operating envelope')}
    before = baseline.build_selection_payload(list(packets.values()), research_question='Which supplied findings answer the task under their stated conditions?')
    after = current.build_selection_payload(list(packets.values()), research_question=before['research_question'])
    old_messages = baseline.selection_messages(before, model_payload=baseline.model_visible_selection_payload(before))
    visible = current.model_visible_selection_payload(after)
    new_messages = current.selection_messages(after, model_payload=visible)
    local = {'status': 'selected', 'groups': [{'unit_ids': ['CH-A_U01', 'CH-A_U02'], 'selection_reason': 'The two local tasks list studies without an explicit comparison duty.', 'improvement_focus': ['Clarify comparison dimensions and attach the relevant conditions to the writing task; verify the gap using source materials.'], 'related_read_only_unit_ids': ['CH-B_U01']}]}
    cross = json.loads(json.dumps(local)); cross['groups'][0]['unit_ids'] = ['CH-A_U01', 'CH-B_U02']
    old_cross = baseline.validate_selection_response(before, cross)
    new_cross = current.validate_selection_response(after, cross)
    checked = current.validate_selection_response(after, local)
    jobs = current.selection_to_on_demand_payloads(packets, checked)
    payload = jobs[0]['payload']
    catalog = demand.build_material_catalog(payload)
    trace = demand.resolve_material_requests(payload, catalog, {'status': 'access_plan', 'material_requests': [{'access_id': 'source_materials[0]', 'unit_ids': ['CH-A_U01']}]})
    access_messages = demand.access_messages(payload, catalog)
    owner_messages = demand.owner_messages(payload, catalog, trace)
    focus = local['groups'][0]['improvement_focus'][0]
    assert focus in json.dumps(access_messages, ensure_ascii=False)
    assert focus in json.dumps(owner_messages, ensure_ascii=False)
    assert before['chapters'][0]['chapter_plan'] == visible['chapters'][0]['chapter_plan']
    assert new_cross['status'] == 'invalid' and old_cross['status'] == 'selected'
    assert current.selection_to_on_demand_payloads(packets, current.validate_selection_response(after, {'status':'none','groups':[]})) == []
    dump('OFFLINE_MESSAGES.json', {'provenance': 'synthetic chapter fixtures; production message constructors; no model calls', 'baseline': BASE, 'before_selector_messages': old_messages, 'after_selector_messages': new_messages, 'controlled_selection_response': local, 'next_access_messages': access_messages, 'next_owner_messages': owner_messages})
    dump('OFFLINE_RESULT.json', {'model_calls':0, 'controlled_return_not_real_model':True, 'before_cross_chapter':old_cross, 'after_cross_chapter':new_cross, 'same_chapter_selection':checked, 'editable_units':payload['modifiable_unit_ids'], 'readonly_units':payload['read_only_unit_ids'], 'focus_in_access_and_owner':True, 'full_plan_equal':True, 'none_creates_no_owner_jobs':True, 'before_message_chars':sum(len(m['content']) for m in old_messages), 'after_message_chars':sum(len(m['content']) for m in new_messages), 'messages_sha256':hashlib.sha256((OUT/'OFFLINE_MESSAGES.json').read_bytes()).hexdigest()})
    print('Offline messages, scope decisions, and real downstream handoff verified')

if __name__ == '__main__':
    main()
