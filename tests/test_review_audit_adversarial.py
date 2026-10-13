"""Independent offline seam checks. No provider or model calls are made."""
from types import SimpleNamespace as NS
from contextlib import closing
import pytest

from optomind_research.runtime.upgrade3 import article_evidence as E
from optomind_research.runtime.upgrade3 import candidate_corpus as C
from optomind_research.runtime.upgrade3 import scholarly_skeleton as S
from test_r01_r02_retrieval_contract import plan, Gateway, response


def test_later_snippet_transport_exception_retains_first_page(tmp_path):
    class FailingLater(Gateway):
        def search_snippets(self, query, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 2:
                raise TimeoutError('later page unavailable')
            return [{'paper': {'corpusId': 7}, 'snippet': {'text': 'Original source passage.'}}] * kwargs['limit'], response()
        def search_papers(self, *args, **kwargs):
            return [], response()
    with closing(C.SnippetTextStore(tmp_path/'snippets.sqlite')) as store:
        result = C.build_candidate_corpus(plan(), gateway=FailingLater(), enrich=False,
            per_query_limit=1, snippet_discovery_target=3, snippet_store=store)
        audit = result['query_audit'][1]
        assert audit['execution_status'] == 'partial'
        assert result['retrieval_hits'] and store.get(result['retrieval_hits'][0]['hit_id']) == 'Original source passage.'
        assert len(audit['responses']) == 2 and not audit['responses'][-1]['ok']


def test_local_reader_cannot_replace_source_or_invent_quote(monkeypatch):
    from optomind_research.runtime.upgrade3 import planning_material_search
    monkeypatch.setattr(planning_material_search, 'search', lambda *a, **kw: NS(hits=[NS(paper_id='p1')]))
    request = E.EvidenceRequest('coefficient at 10 K?', required_outputs=('coefficient',))
    unit = {'canonical_paper_id':'p1','snapshot_id':'s1','source_hash':'a'*64,
            'material_depth':'fulltext','roles':['parameter_prior'],
            'conditions':{'temperature':'10 K'},
            'anchors':[{'source_block_id':'b1','quote':'No coefficient is reported.'}]}
    def reader(**kwargs):
        source = kwargs['source_unit']
        source['anchors'][0]['quote'] = 'The coefficient is 42.'  # In-place mutation must not bypass trust.
        return {**source,'fulfilled_outputs':['coefficient'],'request_binding':E.request_binding(request)}
    with pytest.raises(ValueError, match='reader_quote_not_in_source'):
        E.collect_evidence(request,index=None,source_resolver=lambda hit: unit,directed_reader=reader)
    assert unit['anchors'][0]['quote'] == 'No coefficient is reported.'


def test_routing_label_does_not_count_as_source_read():
    features = {'coverage':{'F1':{'papers_with_hits':1,'read_papers':0,'evidence_sufficient_papers':0}},
                'features':{'p1':{'F1':{}}},'denominator_source':'planned'}
    coverage = S._coverage_with_reading(features, {'p1':{'label':'directly_relevant','routing_validated':True,'read_confirmed':False}})
    assert coverage['read_facet_coverage'] == 0
    assert S._coverage_with_reading(features, {'p1':{'read_confirmed':True,'source_validated':True}})['read_facet_coverage'] == 1


def test_partial_provider_material_does_not_trigger_scientific_revision():
    from test_r05_coverage_repair import runner, request, unit, Gateway as RepairGateway
    class Partial(RepairGateway):
        def search_papers(self, *args, **kwargs):
            hits, _ = super().search_papers(*args, **kwargs)
            return hits, response(category='partial_availability')
        def search_snippets(self, *args, **kwargs):
            return [], response()
    revised = []
    repair = runner(Partial('hit'), source_loader=lambda **kw: unit(kw['request']),
                    query_reviser=lambda **kw: revised.append(kw) or [])
    materials = repair(request=request(), unmet_outputs=('mechanism',))
    assert materials  # Readable earlier material remains useful, but not fulfilled.
    assert not revised  # Transport recovery must not be interpreted as poor scientific wording.
    gaps = [r['gaps'] for r in repair.state['receipts'] if r['operation'] == 'gap']
    assert gaps[0]['F1'] == 'service_failure'


def test_explicit_missing_sidecars_do_not_adopt_stale_directory_files(tmp_path):
    import json
    (tmp_path/'NODE_META.json').write_text(json.dumps({'old-paper':{'abstract':'Unrelated earlier run.'}}))
    corpus = {'run_id':'current-run','plan_hash':'current-plan',
              'bundle':{'artifacts':{'metadata':None,'vectors':None}}}
    assert S._load_enrichment(tmp_path, corpus) == ({}, {})
