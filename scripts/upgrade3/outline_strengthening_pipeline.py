"""Explicit optional selector → on-demand owner → complete-plan handoff.

This is orchestration of the existing modules, not another planner. Default
execution prepares the selector offline. --run uses the existing metered clients;
arrangement and writing remain separate commands consuming the exported root.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import subprocess
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.upgrade3 import outline_strengthening as cli
from optomind_research.runtime.upgrade3 import outline_selection as selection
from optomind_research.runtime.upgrade3 import outline_strengthening as strengthening
from optomind_research.runtime.upgrade3 import progressive_review_plan as planning


def _dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _source_provenance() -> dict:
    files = [Path(__file__), ROOT / 'scripts/upgrade3/outline_strengthening.py'] + [
        ROOT / 'optomind_research/runtime/upgrade3' / (name + '.py')
        for name in ('outline_selection', 'outline_on_demand', 'outline_strengthening',
                     'progressive_review_plan', 'chapter_arrangement', 'review_unit_writer')]
    hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    try:
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True,
                                         stderr=subprocess.DEVNULL, timeout=3).strip()
    except (OSError, subprocess.SubprocessError):
        commit = 'unavailable'
    return {'git_commit': commit, 'executed_source_files_sha256': hashes, 'source_fingerprint': _hash(hashes)}


def load_complete_packets(packet_root: Path) -> tuple[dict, dict[str, dict]]:
    """Use the exact planner manifest, never infer completeness from a group file."""
    plan = _load(packet_root / 'DETAILED_REVIEW_PLAN.json')
    descriptors = plan.get('writer_packets') or []
    if not descriptors:
        raise ValueError('complete_plan_writer_packets_required')
    packets = {}
    for descriptor in descriptors:
        chapter_id = str(descriptor.get('chapter_id') or '').strip()
        if not chapter_id or chapter_id in packets:
            raise ValueError('complete_plan_chapter_identity_invalid')
        path = Path(descriptor.get('json_path') or f'writer_packets/{chapter_id}.json')
        raw = _load(path if path.is_absolute() else packet_root / path)
        actual_id = str(raw.get('chapter_id') or (raw.get('chapter') or {}).get('chapter_id') or '')
        if actual_id and actual_id != chapter_id:
            raise ValueError('complete_plan_packet_identity_mismatch:' + chapter_id)
        raw.setdefault('chapter_id', chapter_id)
        for key in ('research_question', 'shared_outline', 'shared_scope', 'review_argument',
                    'review_argument_status', 'review_argument_source', 'source_identity_map'):
            if not raw.get(key) and plan.get(key):
                raw[key] = copy.deepcopy(plan[key])
        # Keep all packet metadata as well as the explicit strengthening contract.
        envelope = cli._payload(raw)
        packets[chapter_id] = {**copy.deepcopy(raw), **envelope}
        packets[chapter_id]['input_integrity'] = strengthening._input_integrity(packets[chapter_id])
    declared = [str(row.get('chapter_id')) for row in plan.get('shared_outline') or [] if row.get('chapter_id')]
    if declared and set(declared) != set(packets):
        raise ValueError('complete_plan_packet_coverage_mismatch')
    return plan, packets


def export_complete_plan(base_plan: Mapping[str, Any], packets: Mapping[str, Mapping[str, Any]],
                         output: Path, *, provenance: Mapping[str, Any], publish: bool = True) -> Path:
    """Write an immutable complete revision, then publish its explicit pointer."""
    revision = _hash({'packets': packets, 'base_plan': base_plan, 'provenance': provenance})[:20]
    target = output / 'revisions' / revision
    plan = copy.deepcopy(dict(base_plan))
    plan['chapters'] = [copy.deepcopy(dict(row)) for row in packets.values()]
    plan['writer_packets'] = []
    for chapter_id, packet in packets.items():
        # Match the existing formal arrangement filename contract exactly.
        if Path(chapter_id).name != chapter_id or any(c in chapter_id for c in '/\\:'):
            raise ValueError('unsafe_chapter_file_identity:' + chapter_id)
        relative = f'writer_packets/{chapter_id}.json'
        _dump(target / relative, packet)
        plan['writer_packets'].append({'chapter_id': chapter_id, 'json_path': relative,
                                      'source_material_count': len(packet.get('source_materials') or [])})
    plan['outline_strengthening_handoff'] = dict(provenance)
    _dump(target / 'DETAILED_REVIEW_PLAN.json', plan)
    _dump(target / 'HANDOFF_MANIFEST.json', {**dict(provenance), 'revision': revision,
            'packet_root': str(target.resolve()), 'chapter_ids': list(packets),
            'packet_sha256': {key: _hash(value) for key, value in packets.items()}})
    if publish:
        _dump(output / 'CURRENT_PLAN.json', {'packet_root': str(target.resolve()), 'revision': revision,
                                            'manifest': str((target / 'HANDOFF_MANIFEST.json').resolve())})
    return target


def _stage_args(args: argparse.Namespace, *, input_path: Path, output: Path, mode: str) -> argparse.Namespace:
    values = vars(args).copy()
    values.update(input=str(input_path), output=str(output), mode=mode, deduplicate_materials=False)
    return argparse.Namespace(**values)


def _current_group(group: Mapping[str, Any], packets: Mapping[str, Mapping[str, Any]]) -> dict:
    """Resolve only read-only references through earlier accepted split/merge IDs."""
    result = copy.deepcopy(dict(group))
    current_ids = {str(u.get('unit_id') or u.get('id'))
                   for p in packets.values() for u in p['chapter_plan']['units']}
    related = []
    for old in group.get('related_read_only_unit_ids') or []:
        replacements = ([old] if old in current_ids else []) + [
            new for packet in packets.values() for new, ancestors in (packet.get('unit_id_remap') or {}).items()
            if old in ancestors and new in current_ids]
        if not replacements:
            raise ValueError('readonly_reference_unresolved_after_merge:' + str(old))
        related.extend(replacements)
    result['related_read_only_unit_ids'] = list(dict.fromkeys(related))
    return result


def run_pipeline(args: argparse.Namespace) -> dict:
    output = Path(args.output).resolve()
    packet_root = Path(args.packet_root).resolve()
    if output == packet_root or packet_root in output.parents:
        raise ValueError('use_new_output_outside_original_packet_root')
    base_plan, packets = load_complete_packets(packet_root)
    source = _source_provenance()
    baseline_hash = _hash({'plan': base_plan, 'packets': packets})
    state_path = output / 'PIPELINE_INPUT.json'
    if state_path.exists() and _load(state_path).get('input_sha256') != baseline_hash:
        raise ValueError('pipeline_input_changed:use_new_output_to_preserve_existing_results')
    _dump(state_path, {'input_sha256': baseline_hash, 'packet_root': str(packet_root), 'source': source})
    _dump(output / 'run_sources' / (source['source_fingerprint'] + '.json'), source)
    input_path = output / 'COMPLETE_CHAPTER_INPUT.json'
    _dump(input_path, packets)
    selector_dir = output / 'selection'
    selector_args = _stage_args(args, input_path=input_path, output=selector_dir, mode='select')
    cli.prepare_selection(selector_args)
    if not args.run:
        return {'status': 'prepared_no_paid_calls', 'selector_prepare': str(selector_dir / 'PREPARE_REPORT.json'),
                'input_sha256': baseline_hash, 'next': 'Repeat this command with --run and authorized budget/key paths'}
    cli.run_selection_cli(selector_args)
    result = _load(selector_dir / 'SELECTION_RESULT.json')
    selector_payload = _load(selector_dir / 'SELECTION_REQUEST.json')['payload']
    validated = selection.validate_selection_response(selector_payload, result)
    if validated.get('validation_errors'):
        report = {'status': 'selection_unresolved', 'validation_errors': validated['validation_errors']}
        _dump(output / 'PIPELINE_REPORT.json', report)
        return report
    # Validate the full selection before spending on any owner. Projection is
    # rebuilt from the current complete plan at each iteration for fresh roles.
    selection.selection_to_on_demand_payloads(packets, validated)
    previous_pointer = _load(output / 'CURRENT_PLAN.json') if (output / 'CURRENT_PLAN.json').exists() else None
    group_reports = []
    current = copy.deepcopy(packets)
    target = export_complete_plan(base_plan, current, output, provenance={
        'input_sha256': baseline_hash, 'status': 'original_valid_plan', 'groups': []}, publish=previous_pointer is None)
    for group in validated.get('groups') or []:
        group_id = group['group_id']
        stage_dir = output / 'groups' / group_id
        try:
            projection = selection.project_selection_group(current, _current_group(group, current))[0]['payload']
            group_input = stage_dir / 'INPUT.json'
            _dump(group_input, projection)
            stage_args = _stage_args(args, input_path=group_input, output=stage_dir, mode='on_demand')
            cli.prepare_on_demand(stage_args)
            cli.run_on_demand_cli(stage_args)
            owner_result = _load(stage_dir / 'RESULT.json')
            if owner_result.get('status') not in {'updated', 'no_change'}:
                group_reports.append({'group_id': group_id, 'status': 'unresolved', 'result': str(stage_dir / 'RESULT.json')})
                break
            current = strengthening.merge_owner_result_into_chapter_payloads(current, projection, owner_result)
            group_reports.append({'group_id': group_id, 'status': owner_result['status'],
                                  'input_sha256': _hash(projection), 'result': str(stage_dir / 'RESULT.json')})
            target = export_complete_plan(base_plan, current, output, provenance={
                'input_sha256': baseline_hash, 'status': 'accepted_prefix', 'groups': group_reports}, publish=previous_pointer is None)
        except (Exception, SystemExit) as exc:
            # Stage checkpoints keep paid ACCESS/OWNER answers. The published
            # complete plan retains every accepted group and all untouched units.
            group_reports.append({'group_id': group_id, 'status': 'paused', 'reason': str(exc),
                                  'checkpoint_dir': str(stage_dir / 'stages')})
            break
    complete = len(group_reports) == len(validated.get('groups') or []) and all(
        row['status'] in {'updated', 'no_change'} for row in group_reports)
    status = 'complete' if complete else 'partial'
    target = export_complete_plan(base_plan, current, output, provenance={
        'input_sha256': baseline_hash, 'status': status, 'groups': group_reports}, publish=complete or previous_pointer is None)
    report = {'status': status, 'groups': group_reports,
              'packet_root': _load(output / 'CURRENT_PLAN.json')['packet_root'],
              'candidate_packet_root': str(target.resolve()),
              'current_plan': str(output / 'CURRENT_PLAN.json'), 'input_sha256': baseline_hash,
              'previous_valid_plan_preserved': bool(previous_pointer and not complete),
              'adopted_packet_root': _load(output / 'CURRENT_PLAN.json')['packet_root'],
              'chapter_count': len(current), 'arrangement_not_run': True, 'source': source,
              'resume': 'Repeat the same command to reuse valid stages; inspect unresolved responses before explicit --retry-unresolved'}
    _dump(output / 'PIPELINE_REPORT.json', report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packet-root', required=True, help='Complete planner output with writer_packets manifest')
    parser.add_argument('--output', required=True, help='Separate optional-module run directory')
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--retry-unresolved', action='store_true', help='Explicitly retry a saved invalid response once, preserving valid paid stages')
    parser.add_argument('--profile', default=cli.DEFAULT_PROFILE)
    parser.add_argument('--access-profile', default=cli.DEFAULT_ACCESS_PROFILE)
    parser.add_argument('--selection-profile', default=cli.DEFAULT_SELECTION_PROFILE)
    parser.add_argument('--include-selection-material-index', action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument('--on-demand-stream', action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument('--on-demand-request-timeout', type=float, default=1800.0)
    parser.add_argument('--on-demand-stream-overall-timeout', type=float, default=3600.0)
    parser.add_argument('--tokenizer', default='')
    parser.add_argument('--budget-ledger', default='')
    parser.add_argument('--budget-limit', type=float, default=30.0)
    parser.add_argument('--key-file', default='')
    args = parser.parse_args(argv)
    report = run_pipeline(args)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['status'] in {'complete', 'prepared_no_paid_calls'} else 2


if __name__ == '__main__':
    raise SystemExit(main())
