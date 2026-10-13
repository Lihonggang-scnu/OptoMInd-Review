"""Free CLI dispatch checks with real planning/stage persistence and fake models."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.upgrade3 import progressive_review_plan as cli
from scripts.upgrade3 import outline_strengthening_pipeline as pipeline
from scripts.upgrade3 import chapter_arrangement as arrangement
from scripts.upgrade3 import fullbody_writer as body_manifest
from optomind_research.runtime.upgrade3.module4.runtime import GlobalBudgetLedger
from test_outline_promotion_pipeline import fixture_root, fake_factory


def setup_cli(tmp_path, monkeypatch):
    _, packet = fixture_root(tmp_path)
    plan = tmp_path / 'PLAN.json'
    pool = tmp_path / 'POOL.jsonl'
    card = tmp_path / 'CARD.json'
    pipeline._dump(plan, {'question': packet['research_question'], 'facets': [{'facet_id': 'F1', 'question': 'Optical response'}]})
    source = packet['source_materials'][0]
    pipeline._dump(card, {'paper_identity': {'canonical_paper_id': source['paper_id'], 'title': source['title']},
                         'general_understanding': source['study_summary_A'], 'review_planning': source['review_planning_B']})
    pool.write_text(json.dumps({'paper_id': source['paper_id'], 'title': source['title'], 'doi': source['doi'],
        'card_path': str(card), 'planning_view': {'paper_identity': {'canonical_paper_id': source['paper_id']},
        **source['review_planning_B']}, 'summary_view': source['study_summary_A']}) + '\n', encoding='utf-8')
    calls = []

    class OfflinePlanner:
        def __init__(self, **kw):
            self.model = kw['model']; self.chapter_model = kw['chapter_model']
            self.thinking_budget = kw['thinking_budget']; self.output_tokens = kw['output_tokens']
            self.ledger = GlobalBudgetLedger(path=kw['budget_ledger_path'], limit_cny=kw['budget_limit_cny'])

        def __call__(self, stage, payload):
            calls.append(stage)
            reservation = self.ledger.reserve(.01, 'offline-planning:' + stage)
            answer = self.answer(stage, payload)
            self.ledger.settle(reservation['reservation_id'], .01)
            return answer

        def answer(self, stage, payload):
            chapter = {'chapter_id': 'CH01', 'title': 'Mechanisms', 'purpose': 'Explain conditional response', 'scope': 'Optics'}
            if stage == 'provisional_scope':
                return {'provisional_outline': [chapter], 'material_theme_inventory': ['optics']}
            if stage == 'level1_outline':
                return {'shared_outline': [chapter]}
            if stage == 'source_routing':
                return {'source_routes': [{'source_handle': 'P0001', 'chapter_ids': ['CH01'], 'specific_usable_material': 'Conditional optical response'}]}
            if stage == 'chapter_proposals':
                return {'chapter_proposals': [chapter]}
            if stage in {'harmonize_scope', 'finalize_chapter_scope'}:
                return {'shared_outline': [chapter], 'chapters': [chapter]}
            if stage == 'chapter_details':
                return {'chapter_plan': copy.deepcopy(packet['chapter_plan'])}
            if stage == 'whole_plan_improvement':
                return {'affected_chapters': []}
            if stage == 'case_groups':
                return {'additions': []}
            raise AssertionError(stage)

    monkeypatch.setattr(cli.planning, 'QwenProgressivePlanner', OfflinePlanner)
    for name in ('make_planning_supplement_runner', 'make_directed_reading_runner', 'make_retrieval_loop_runner'):
        monkeypatch.setattr(cli.planning, name, lambda *a, **kw: None)
    out = tmp_path / 'planning'
    ledger = tmp_path / 'shared.sqlite'
    # Real durable budget schema, with all model calls replaced before execution.
    GlobalBudgetLedger(path=ledger, limit_cny=30.)
    argv = ['--plan', str(plan), '--pool', str(pool), '--output-dir', str(out), '--run',
            '--topic-id', 'offline-cli', '--chapter-workers', '1', '--reader-workers', '1',
            '--tokenizer', str(tmp_path / 'absent-tokenizer.json'), '--no-external-supplement',
            '--key-file', str(tmp_path / 'unused-key-path'), '--budget-ledger', str(ledger), '--budget-limit-cny', '30']
    return argv, out, packet, calls


def result(capsys):
    return json.loads(capsys.readouterr().out)


def metered_fake_factory(monkeypatch, packet, **options):
    calls = fake_factory(monkeypatch, packet, **options)
    fake = pipeline.cli.strengthening.make_strengthening_client

    def factory(*, role, budget_ledger, **kw):
        client = fake(role=role, budget_ledger=budget_ledger, **kw)

        def metered(messages, **effective):
            reservation = budget_ledger.reserve(.01, 'offline-strengthening:' + role)
            try:
                response = client(messages, **effective)
            except Exception:
                budget_ledger.settle(reservation['reservation_id'], None, uncertain=True)
                raise
            budget_ledger.settle(reservation['reservation_id'], .01)
            return response
        return metered

    monkeypatch.setattr(pipeline.cli.strengthening, 'make_strengthening_client', factory)
    return calls


def test_default_cli_dispatches_existing_pipeline_and_exports_complete_pointer(tmp_path, monkeypatch, capsys):
    argv, out, packet, planning_calls = setup_cli(tmp_path, monkeypatch)
    strengthening_calls = metered_fake_factory(monkeypatch, packet)
    assert cli.main(argv) == 0
    report = result(capsys)
    assert report['outline_strengthening_enabled'] is True
    assert report['outline_strengthening']['status'] == 'complete'
    assert [row[0] for row in strengthening_calls] == ['outline_selection', 'autonomous_outline', 'strong_outline']
    adopted = pipeline._load(out / 'CURRENT_PLAN.json')
    assert report['packet_root'] == adopted['packet_root']
    final = pipeline._load(Path(report['packet_root']) / 'writer_packets/CH01.json')
    original = pipeline._load(out / 'writer_packets/CH01.json')
    assert final['chapter_plan']['units'][1:] == original['chapter_plan']['units'][1:]
    assert len(final['chapter_plan']['units']) == 3
    assert report['budget_after']['actual_cny'] == pytest.approx(.01 * (len(planning_calls) + 3))
    assert arrangement.main(['--packet-root', report['packet_root'], '--output-root', str(tmp_path / 'arranged'), '--chapter', 'CH01']) == 0
    capsys.readouterr()
    before = (len(planning_calls), len(strengthening_calls))
    assert cli.main(argv + ['--resume']) == 0
    again = result(capsys)
    assert (len(planning_calls), len(strengthening_calls)) == before
    assert again['packet_root'] == report['packet_root']
    assert again['budget_after']['actual_cny'] == report['budget_after']['actual_cny']


@pytest.mark.parametrize('stop', ['level1', 'level2'])
def test_cli_stage_stop_does_not_enter_strengthening(tmp_path, monkeypatch, capsys, stop):
    argv, out, packet, _ = setup_cli(tmp_path, monkeypatch)
    calls = fake_factory(monkeypatch, packet)
    assert cli.main(argv + ['--stop-after', stop]) == 0
    report = result(capsys)
    assert report['outline_strengthening']['status'] == 'not_run_stage_stop'
    assert not calls and not (out / 'CURRENT_PLAN.json').exists()


def test_cli_explicit_disable_exports_base_pointer(tmp_path, monkeypatch, capsys):
    argv, out, packet, _ = setup_cli(tmp_path, monkeypatch)
    calls = fake_factory(monkeypatch, packet)
    assert cli.main(argv + ['--no-outline-strengthening']) == 0
    report = result(capsys)
    assert not calls
    assert report['outline_strengthening_enabled'] is False
    assert report['packet_root'] != str(out.resolve())
    assert pipeline._load(out / 'CURRENT_PLAN.json')['packet_root'] == report['packet_root']
    assert body_manifest._resolve_root(out, body_manifest._Files()) == Path(report['packet_root'])
    assert body_manifest._resolve_root(out / 'CURRENT_PLAN.json', body_manifest._Files()) == Path(report['packet_root'])
    assert pipeline._load(Path(report['packet_root']) / 'writer_packets/CH01.json')['chapter_plan'] == pipeline._load(out / 'writer_packets/CH01.json')['chapter_plan']


def test_cli_enable_disable_resume_selects_the_requested_complete_revision(tmp_path, monkeypatch, capsys):
    argv, out, packet, planning_calls = setup_cli(tmp_path, monkeypatch)
    calls = metered_fake_factory(monkeypatch, packet)
    assert cli.main(argv) == 0
    enabled = result(capsys)
    assert cli.main(argv + ['--resume', '--no-outline-strengthening']) == 0
    disabled = result(capsys)
    assert disabled['packet_root'] != enabled['packet_root']
    assert body_manifest._resolve_root(out, body_manifest._Files()) == Path(disabled['packet_root'])
    unstrengthened = pipeline._load(Path(disabled['packet_root']) / 'writer_packets/CH01.json')
    assert 'matched angle' not in unstrengthened['chapter_plan']['units'][0]['paragraph_briefs'][0]['development']
    before = (len(planning_calls), len(calls))
    assert cli.main(argv + ['--resume']) == 0
    restored = result(capsys)
    assert restored['packet_root'] == enabled['packet_root']
    assert body_manifest._resolve_root(out, body_manifest._Files()) == Path(restored['packet_root'])
    assert (len(planning_calls), len(calls)) == before


def test_cli_selection_none_is_autonomous_completion(tmp_path, monkeypatch, capsys):
    argv, out, packet, _ = setup_cli(tmp_path, monkeypatch)
    calls = fake_factory(monkeypatch, packet, empty=True)
    assert cli.main(argv) == 0
    report = result(capsys)
    assert report['outline_strengthening']['status'] == 'complete'
    assert [row[0] for row in calls] == ['outline_selection']
    assert pipeline._load(Path(report['packet_root']) / 'writer_packets/CH01.json')['chapter_plan'] == pipeline._load(out / 'writer_packets/CH01.json')['chapter_plan']


def test_cli_failed_owner_saves_base_and_resume_reuses_planning_selection_access(tmp_path, monkeypatch, capsys):
    argv, out, packet, planning_calls = setup_cli(tmp_path, monkeypatch)
    first = metered_fake_factory(monkeypatch, packet, fail_owner=True)
    assert cli.main(argv) == 2
    failed = result(capsys)
    assert failed['status'] == 'outline_strengthening_partial'
    assert failed['planning_status'] == 'complete'
    assert failed['budget_after']['held_cny'] == pytest.approx(.01)
    assert failed['packet_root'] == pipeline._load(out / 'CURRENT_PLAN.json')['packet_root']
    assert pipeline._load(Path(failed['packet_root']) / 'writer_packets/CH01.json')['chapter_plan'] == pipeline._load(out / 'writer_packets/CH01.json')['chapter_plan']
    before = len(planning_calls)
    second = metered_fake_factory(monkeypatch, packet)
    assert cli.main(argv + ['--resume']) == 0
    resumed = result(capsys)
    assert resumed['outline_strengthening']['status'] == 'complete'
    assert len(planning_calls) == before
    assert [row[0] for row in second] == ['strong_outline']
    assert [row[0] for row in first] == ['outline_selection', 'autonomous_outline', 'strong_outline']
    assert resumed['budget_after']['actual_cny'] - failed['budget_after']['actual_cny'] == pytest.approx(.01)
    assert resumed['budget_after']['held_cny'] == failed['budget_after']['held_cny']


@pytest.mark.parametrize('failure', ['budget', 'invalid_selection'])
def test_cli_selector_failure_has_complete_saved_plan_and_no_owner(tmp_path, monkeypatch, capsys, failure):
    argv, out, packet, planning_calls = setup_cli(tmp_path, monkeypatch)
    calls = []

    def factory(*, role, budget_ledger, **kw):
        def client(messages, **effective):
            calls.append(role)
            if failure == 'budget':
                budget_ledger.reserve(31., 'controlled-insufficient-balance')
            return {'content': json.dumps({'status': 'selected', 'groups': [{'unit_ids': ['MISSING']}]}), 'complete': True, 'finish_reason': 'stop'}
        return client

    monkeypatch.setattr(pipeline.cli.strengthening, 'make_strengthening_client', factory)
    assert cli.main(argv) == 2
    failed = result(capsys)
    assert failed['outline_strengthening']['status'] != 'complete'
    assert calls == ['outline_selection']
    assert len(pipeline._load(Path(failed['packet_root']) / 'DETAILED_REVIEW_PLAN.json')['writer_packets']) == 1
    assert (out / 'CURRENT_PLAN.json').is_file()
    assert failed['budget_after']['actual_cny'] == pytest.approx(.01 * len(planning_calls))
    if failure == 'invalid_selection':
        assert cli.main(argv + ['--resume']) == 2
        again = result(capsys)
        assert calls == ['outline_selection']  # invalid paid answer is saved, never silently retried
        assert again['budget_after'] == failed['budget_after']
        assert cli.main(argv + ['--resume', '--retry-unresolved']) == 2
        result(capsys)
        assert calls == ['outline_selection', 'outline_selection']


def test_cli_selector_budget_failure_preserves_previous_success(tmp_path, monkeypatch, capsys):
    argv, out, packet, _ = setup_cli(tmp_path, monkeypatch)
    fake_factory(monkeypatch, packet)
    assert cli.main(argv) == 0
    original = result(capsys)
    original_pointer = pipeline._load(out / 'CURRENT_PLAN.json')
    messages = pipeline.selection.selection_messages
    monkeypatch.setattr(pipeline.selection, 'selection_messages', lambda *a, **kw: messages(*a, **kw) + [{'role': 'system', 'content': 'Changed selector contract fixture'}])

    def factory(*, budget_ledger, **kw):
        def client(*a, **kw):
            budget_ledger.reserve(31., 'controlled-budget-refusal')
        return client

    monkeypatch.setattr(pipeline.cli.strengthening, 'make_strengthening_client', factory)
    assert cli.main(argv + ['--resume']) == 2
    failed = result(capsys)
    assert failed['outline_strengthening']['previous_valid_plan_preserved'] is True
    assert failed['packet_root'] == original['packet_root']
    assert pipeline._load(out / 'CURRENT_PLAN.json')['revision'] == original_pointer['revision']


def test_cli_partial_scientific_revision_with_complete_packets_still_strengthens(tmp_path, monkeypatch, capsys):
    argv, out, packet, _ = setup_cli(tmp_path, monkeypatch)
    assemble = cli.planning.ProgressiveReviewPlanner._assemble_final

    def partial(self, **kw):
        plan = assemble(self, **kw)
        plan['status'] = 'partial'
        plan['owner_revision_unresolved'] = ['CH01']
        return plan

    monkeypatch.setattr(cli.planning.ProgressiveReviewPlanner, '_assemble_final', partial)
    calls = fake_factory(monkeypatch, packet)
    assert cli.main(argv) == 2
    report = result(capsys)
    assert report['planning_status'] == report['status'] == 'partial'
    assert report['outline_strengthening']['status'] == 'complete'
    assert len(calls) == 3
    assert pipeline._load(out / 'CURRENT_PLAN.json')['planning_status'] == 'partial'


@pytest.mark.parametrize('missing', ['chapter', 'tasks'])
def test_cli_incomplete_packet_structure_stops_before_selector(tmp_path, monkeypatch, capsys, missing):
    argv, out, packet, _ = setup_cli(tmp_path, monkeypatch)
    write = cli.planning.ProgressiveReviewPlanner._write_final_outputs

    def incomplete(self, plan):
        write(self, plan)
        path = out / 'DETAILED_REVIEW_PLAN.json' if missing == 'chapter' else out / 'writer_packets/CH01.json'
        saved = pipeline._load(path)
        if missing == 'chapter':
            saved['shared_outline'].append({'chapter_id': 'CH02', 'title': 'Missing chapter'})
        else:
            saved['chapter_plan']['units'] = []
        pipeline._dump(path, saved)

    monkeypatch.setattr(cli.planning.ProgressiveReviewPlanner, '_write_final_outputs', incomplete)
    calls = fake_factory(monkeypatch, packet)
    assert cli.main(argv) == 1
    captured = capsys.readouterr()
    failure = json.loads(captured.err)
    assert 'complete_plan_' in failure['error']
    assert not calls and (out / 'RUN_FAILURE.json').is_file()
    state = pipeline._load(out / 'RUN_STATE.json')
    assert state['status'] == 'failed'
    assert state['last_error'] == failure['error']
    assert state['current_stage'] == ''


def test_cli_preflight_does_not_dispatch_or_open_key(tmp_path, monkeypatch, capsys):
    argv, out, packet, planning_calls = setup_cli(tmp_path, monkeypatch)
    calls = fake_factory(monkeypatch, packet)
    monkeypatch.setattr(cli.planning, 'qwen_local_token_counter', lambda path: lambda *a: 10)
    argv.remove('--run')
    assert cli.main(argv) == 0
    report = result(capsys)
    assert report['outline_strengthening_enabled'] is True
    assert report['status'] == 'preflight_only' and report['network_calls'] == 0
    assert report['missing_card_paths'] == []
    assert not planning_calls and not calls and not out.exists()
