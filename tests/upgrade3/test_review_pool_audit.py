"""Published card versions must stay bound at the real planning consumer."""
import json
from pathlib import Path
import pytest
from test_review_material_boundaries import batch_fixture
from optomind_research.runtime.upgrade3 import planning_pool_adapter as pool
from optomind_research.runtime.upgrade3.progressive_review_plan import load_planning_pool


def test_pool_rejects_snapshot_manifest_change_after_publication(tmp_path):
    state, item = batch_fixture(tmp_path)
    report = pool.publish_planning_pool(batch_state_path=state, output_dir=tmp_path/'pool')
    path = Path(item['snapshot_dir'])/'DOCUMENT_MANIFEST.json'
    manifest = json.loads(path.read_text())
    # Operational metadata does not alter snapshot_id, but is part of the exact
    # card input hash. It must not silently replace the selected input version.
    manifest['source_documents'][0]['retrieved_at'] = '2030-01-01T00:00:00Z'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='selected_snapshot_hash_mismatch'):
        load_planning_pool(report['pool_path'])


def test_source_unit_retains_snapshot_block_locator(tmp_path):
    from test_reader_helper_token_budgets import card_inputs
    from optomind_research.runtime.upgrade3.article_evidence import source_unit_from_snapshot
    from optomind_research.runtime.upgrade3.local_materials import PreparedSnapshotProvider
    inputs = card_inputs(tmp_path)
    snapshot = PreparedSnapshotProvider(inputs['snapshot_dir']).load()
    unit = source_unit_from_snapshot(snapshot_dir=inputs['snapshot_dir'], roles=['parameter_prior'])
    blocks = {b['block_id']: b for b in snapshot.blocks}
    assert any(blocks[a['source_block_id']]['locator'] for a in unit['anchors'])
    for anchor in unit['anchors']:
        assert anchor['source_locator'] == blocks[anchor['source_block_id']]['locator']


def test_real_selected_pool_supplies_verified_read_coverage(tmp_path):
    from optomind_research.runtime.upgrade3 import scholarly_skeleton as skeleton
    state, _ = batch_fixture(tmp_path)
    report = pool.publish_planning_pool(batch_state_path=state, output_dir=tmp_path/'pool')
    rows = load_planning_pool(report['pool_path'])
    assert rows[0]['read_confirmed'] and rows[0]['source_validated']
    canonical = rows[0]['canonical_paper_id']
    verified = skeleton._verified_pool_reading(report['pool_path'], {canonical:'node-1'})
    features = {'coverage':{'F1':{'papers_with_hits':1,'read_papers':0,'evidence_sufficient_papers':0}},
                'features':{'node-1':{'F1':{}}},'denominator_source':'planned'}
    coverage = skeleton._coverage_with_reading(features, verified)
    assert coverage['read_facet_coverage'] == 1
    assert coverage['evidence_sufficient_facet_coverage'] == 0
