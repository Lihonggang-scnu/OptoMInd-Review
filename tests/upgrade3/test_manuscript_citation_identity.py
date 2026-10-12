from optomind_research.runtime.upgrade3.delivery_citations import build_delivery_citation_map


def test_namespace_fallback_keeps_distinct_papers_and_primary_alias():
    catalogs = [
        {'namespace': 'left', 'references': [{'handles': ['P0001', 'P0002'], 'title': 'Source A'}]},
        {'namespace': 'right', 'references': [{'handles': ['P0001'], 'title': 'Source B'}]},
    ]
    result = build_delivery_citation_map(
        final_handle_draft='[P0001] [left::P0002] [right::P0001]', identity_catalogs=catalogs)
    detail = result['token_details']
    assert detail['P0001']['number'] == detail['left::P0002']['number'] == 1
    assert detail['right::P0001']['number'] == 2
    assert detail['P0001']['canonical'] == 'handle:left::P0001'
    assert detail['right::P0001']['canonical'] == 'handle:right::P0001'


def test_single_handle_field_does_not_merge_unrelated_unknown_identities():
    result = build_delivery_citation_map(final_handle_draft='[P0001] [P0002]',
        identity_catalogs=[{'references': [
            {'source_handle': 'P0001', 'title': 'Source A'},
            {'source_handle': 'P0002', 'title': 'Source B'}]}])
    assert len(result['references']) == 2
    assert result['token_details']['P0001']['canonical'] == 'handle:P0001'
    assert result['token_details']['P0002']['canonical'] == 'handle:P0002'


def test_strong_identity_still_merges_aliases_across_namespaces():
    result = build_delivery_citation_map(final_handle_draft='[a::P0001] [b::P0009]',
        identity_catalogs=[
            {'namespace': 'a', 'references': [{'handles': ['P0001'], 'doi': '10.1234/example'}]},
            {'namespace': 'b', 'references': [{'handles': ['P0009'], 'doi': '10.1234/EXAMPLE'}]},
        ])
    assert len(result['references']) == 1
    assert result['token_details']['a::P0001']['number'] == result['token_details']['b::P0009']['number']
