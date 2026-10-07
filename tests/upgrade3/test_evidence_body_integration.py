"""Independent formal-consumer replay of the locked seven-route archive.

Historical responses are replayed, never presented as new generations. Controlled
insertions exercise only contracts and must not count as scientific quality proof.
Only the model boundary is substituted; store, stage executor and parsers are real.
"""
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from optomind_research.runtime.upgrade3 import evidence_body_writer as engine
from optomind_research.runtime.upgrade3 import writing_evidence as compiler
from optomind_research.runtime.upgrade3.fullbody_contracts import fullbody_task_catalog, project_fullbody

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / 'docs/acceptance/fullbody-seven-routes-20261008'
ARCHIVE_COMMIT = 'a5df7195ed90da5278011a6efd38ea2ef84690a4'


def canonical(raw, size, sha):
    if len(raw) == size + 2 and raw.endswith(b'\r\n'):
        raw = raw[:-2]
    assert len(raw) == size and hashlib.sha256(raw).hexdigest() == sha
    return raw


@lru_cache(None)
def archived(relative):
    path = ARCHIVE / relative
    if path.exists():
        return json.loads(path.read_bytes())
    manifest = json.loads(Path(str(path) + '.parts.json').read_bytes())
    raw = b''.join(canonical((ARCHIVE / item['path']).read_bytes(), item['bytes'], item['sha256'])
                   for item in manifest['parts'])
    return json.loads(canonical(raw, manifest['source_bytes'], manifest['source_sha256']))


def stage_raw(route, stage):
    paths = sorted((ARCHIVE / route / 'stages' / stage).glob('*/attempt_001/RAW_RESPONSE.json*'))
    assert paths, (route, stage)
    path = str(paths[0].relative_to(ARCHIVE)).split('.json')[0] + '.json'
    return deepcopy(archived(path))


def config(**extra):
    role = dict(model='qwen3.5-plus', thinking=True, thinking_budget=16384,
                max_output_tokens=49152, stream=True, json_mode=False,
                timeout_seconds=1800, stream_overall_timeout_seconds=3600,
                prompt_token_multiplier=1.12, prompt_token_framing_margin=8192)
    return {**{r: deepcopy(role) for r in ('writer', 'curator', 'reader', 'reviser')}, **extra}


def meter(raw, messages):
    # Deliberate offline capacity fixture, not a tokenizer or cost measurement.
    return 1000


class ReplayBoundary:
    execution_mode = 'recording'
    fixture_sha256 = 'locked-seven-route-real-raw-replay-v1'

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, role, directory, profile):
        def call(messages, **kwargs):
            assert (directory / 'MESSAGES.json').exists()
            assert (directory / 'ACTUAL_REQUEST.json').exists()
            payload = json.loads(messages[-1]['content'])
            self.calls.append({'role': role, 'payload': payload, 'messages': messages})
            response = self.responses[len(self.calls) - 1]
            if isinstance(response, Exception):
                raise response
            if callable(response):
                response = response(payload)
            return deepcopy(response)
        return call


def envelope(value):
    return {'content': json.dumps(value, ensure_ascii=False), 'complete': True,
            'finish_reason': 'stop', 'usage': {'prompt_tokens': 100, 'completion_tokens': 30}}


class EvidenceArchiveIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = archived('plain_whole/FULL_BODY_INPUT.json')
        cls.pack = compiler.compile_evidence(cls.book)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / 'out'
        def deny(*args, **kwargs):
            raise AssertionError('Archive integration must never reach network')
        self.addCleanup(patch.stopall)
        patch.object(socket.socket, 'connect', deny).start()
        patch.object(socket, 'create_connection', deny).start()

    def run_route(self, factory, **settings):
        return engine.run_evidence_body(deepcopy(self.book), route='packed_continuous',
            output_dir=self.output, config=config(**settings), run=True,
            client_factory=factory, counter=meter)

    def test_real_continuous_raw_reaches_seven_chapters_and_reuses_cache(self):
        factory = ReplayBoundary([stage_raw('live_continuous_author', f'author_{i:03d}') for i in range(1, 8)])
        result = self.run_route(factory)
        self.assertTrue(result['complete'], result['issues'])
        self.assertEqual(result['completed_task_ids'], list(fullbody_task_catalog(self.book)))
        self.assertEqual(len(result['segments']), 7)
        original = archived('live_continuous_author/FULL_BODY_RESULT.json')
        self.assertEqual(result['body_markdown'], original['body_markdown'])
        self.assertEqual(result['body_sha256'], original['body_sha256'])
        for i, call in enumerate(factory.calls):
            self.assertEqual(call['payload']['accepted_body_markdown'], '\n\n'.join(
                s['body_markdown'] for s in result['segments'][:i]))
        self.run_route(factory)
        self.assertEqual(len(factory.calls), 7, 'same-code resume must not replay model boundary')

    def test_actual_workbench_missing_table_preserved_on_failed_completion(self):
        raw = stage_raw('live_workbench', 'author_001')
        factory = ReplayBoundary([raw, RuntimeError('controlled completion interruption')])
        result = self.run_route(factory)
        self.assertFalse(result['complete'])
        self.assertTrue(result['partial_unaccepted_prose'])
        self.assertEqual(result['body_markdown'], archived('live_workbench/FULL_BODY_RESULT.json')['body_markdown'])
        self.assertEqual(len(factory.calls), 2)
        missing = factory.calls[1]['payload']['missing_task_ids']
        self.assertEqual(len(missing), 1)
        self.assertTrue(missing[0].endswith('_T01'))
        self.assertTrue((self.output / 'FULL_BODY.md').exists())

    def test_actual_workbench_raw_plus_controlled_table_completes_formal_scope(self):
        # The table is intentionally a test fixture, NOT scientific supplementation.
        marker = '\n\n| Controlled fixture | Contract only |\n| --- | --- |\n| Fixture row | Not quality evidence |\n'
        def completion(payload):
            return envelope({'insertions': [{'after_anchor': '', 'text': marker}],
                'completed_task_ids': payload['missing_task_ids'], 'complete': True})
        responses = [stage_raw('live_workbench', 'author_001'), completion]
        responses += [stage_raw('live_continuous_author', f'author_{i:03d}') for i in range(2, 8)]
        factory = ReplayBoundary(responses)
        result = self.run_route(factory)
        self.assertTrue(result['complete'], result['issues'])
        self.assertEqual(len(factory.calls), 8)
        self.assertEqual(result['segments'][0]['body_markdown'],
            archived('live_workbench/FULL_BODY_RESULT.json')['body_markdown'] + marker)
        self.assertEqual(result['completed_task_ids'], list(fullbody_task_catalog(self.book)))

    def test_actual_compiler_preserves_selected_tasks_and_coupled_science(self):
        self.assertEqual(self.pack['canonical_book'], self.book)
        for chapter in self.book['chapters']:
            ids = [tid for tid, row in self.pack['tasks'].items() if row['chapter_id'] == chapter['chapter_id']]
            payload = compiler.evidence_payload(self.pack, ids)
            self.assertEqual(payload['editable_task_ids'], ids)
            for tid in ids:
                self.assertEqual(payload['tasks'][tid], self.pack['tasks'][tid])
            for atom in payload['evidence_atoms']:
                self.assertEqual(atom['value'], self.pack['atoms'][atom['atom_id']]['value'])
        # A real FMT finding has conditions co-located, never clipped or detached.
        findings = [a for a in self.pack['atoms'].values()
                    if a['source_handle'] == 'P0583' and a['field_path'] == ['study_summary_A', 'key_findings']]
        self.assertTrue(findings)
        self.assertTrue(any(isinstance(a['value'], dict) and a['value'].get('conditions') for a in findings))

    def test_actual_reader_failed_anchor_is_zero_match_not_transport(self):
        raw = stage_raw('live_reader_revision', 'revision_002')
        obj = json.loads(raw['content'])
        base = archived('live_continuous_author/FULL_BODY_RESULT.json')['body_markdown']
        self.assertEqual([base.count(p['anchor']) for p in obj['patches']], [1, 0])
        self.assertEqual(raw['finish_reason'], 'stop')

    def test_archived_reader_findings_adapted_to_ids_preserve_two_valid_repairs(self):
        from optomind_research.runtime.upgrade3 import scoped_body_revision as sr
        base = deepcopy(archived('live_continuous_author/FULL_BODY_RESULT.json'))
        # Public archive path normalization changes its storage hash. This explicit
        # offline fixture binds unchanged archived science/tasks to this snapshot.
        base['historical_input_hash'] = base['input_hash']
        base['input_hash'] = sr._hash(self.book)
        body = base['body_markdown']
        blocks = sr.index_blocks(body, self.book, base)
        old_issues = sr.fw._object(stage_raw('live_reader_revision', 'reader_full_body'))['issues']
        mapped = []
        for issue in old_issues:
            matches = [b for b in blocks if issue['anchor'] in b['content']]
            self.assertEqual(len(matches), 1)
            mapped.append({'issue_id': issue['issue_id'], 'target_block_ids': [matches[0]['block_id']],
                'related_task_ids': [], 'source_handles': [], 'reason': issue['problem'],
                'goal': issue['suggested_action']})
        old_patches = sr.fw._object(stage_raw('live_reader_revision', 'revision_001'))['patches']
        def real_patch(payload):
            issue_id = payload['reader_issues'][0]['issue_id']
            patch = next(p for p in old_patches if p['issue_id'] == issue_id)
            block = payload['editable_blocks'][0]
            return envelope({'complete': True, 'groups': [{'issue_id': issue_id,
                'action': 'replace_block', 'original_body_sha256': sr.fw._text_hash(body),
                'replacements': [{'block_id': block['block_id'], 'original_sha256': block['sha256'],
                    'content': block['content'].replace(patch['anchor'], patch['replacement'])}]}]})
        # Preserve actual bad-anchor counterexample as a rejected edit group. No
        # approximate mapping or invented scientific replacement is allowed.
        def unresolved(payload):
            block = payload['editable_blocks'][0]
            return envelope({'complete': True, 'groups': [{'issue_id': mapped[2]['issue_id'],
                'action': 'replace_block', 'original_body_sha256': sr.fw._text_hash(body),
                'replacements': [{'block_id': block['block_id'], 'original_sha256': 'nonexistent-original-anchor',
                    'content': block['content']}]}]})
        factory = ReplayBoundary([envelope({'complete': True, 'issues': mapped}), real_patch, real_patch, unresolved])
        settings = config(); settings.pop('curator')
        result = sr.run_scoped_revision(self.book, base, self.output, settings,
            client_factory=factory, run=True, counter=meter)
        self.assertTrue(result['body_complete'])
        self.assertFalse(result['revision_complete'])
        self.assertEqual(len(result['accepted_groups']), 2)
        expected = body
        for patch in old_patches:
            expected = expected.replace(patch['anchor'], patch['replacement'])
        self.assertEqual(result['body_markdown'], expected)
        self.assertEqual(result['body_markdown'], archived('live_reader_revision/FULL_BODY_RESULT.json')['body_markdown'])
        self.assertTrue(result['pending_issues'])
        self.assertEqual(len(factory.calls), 4)
        self.assertFalse(result['cost_summary']['base_reuse_charged_again'])

    def test_cross_domain_alias_unknown_scope_and_nested_review_are_retrievable(self):
        book = {'research_question': 'Do measured film recoveries establish lifetime?', 'chapters': [{
            'chapter_id': 'Materials', 'chapter_frame': {'title': 'Reversible recovery'},
            'units': [{'unit_id': 'U', 'paragraph_tasks': [{'paragraph_id': 'T',
                'point': 'Compare recovery with lifetime', 'source_handles': ['ALIAS'],
                'custom_condition': {'temperature_C': -10, 'window_seconds': 60}}], 'table_tasks': []}],
            'sources': [{'source_handle': 'P1', 'aliases': ['ALIAS'],
                'study_summary_A': {'key_findings': [{'finding': 'Signal recovers',
                    'conditions': {'temperature_C': -10, 'window_seconds': 60}}],
                    'conditions': {'humidity': 'dry only'},
                    'unknown_scope': {'excluded_outcome': 'field lifetime'}},
                'new_wrapper': {'evidence_origin': {'review_source_handle': 'R1'},
                    'scope': {'not_primary_experiment': True}}},
                {'source_handle': 'R1', 'title': 'Review native record',
                    'new_review_science': {'interpretation': 'Recovery is not service lifetime',
                        'conditions': {'method': 'secondary synthesis'}}}]}]}
        pack = compiler.compile_evidence(book)
        self.assertEqual(pack['canonical_book'], book)
        tid = next(iter(pack['tasks']))
        self.assertEqual(set(pack['task_source_handles'][tid]), {'P1', 'R1'})
        aid = next(a['atom_id'] for a in pack['atoms'].values()
            if a['source_handle'] == 'P1' and a['field_path'] == ['study_summary_A', 'key_findings'])
        selected = compiler.select_atoms(pack, [tid], [aid])
        rendered = json.dumps(selected, ensure_ascii=False)
        self.assertIn('dry only', rendered)
        self.assertIn('field lifetime', rendered)
        self.assertIn('secondary synthesis', rendered)
        self.assertEqual(selected['source_aliases']['ALIAS'], 'P1')


if __name__ == '__main__':
    unittest.main()
