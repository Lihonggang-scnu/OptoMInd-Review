"""Local publication language/metadata regression; no provider or compiler."""
from optomind_research.runtime.upgrade3.review_delivery import _extract_front_matter, _verify_reader_draft
from optomind_research.runtime.upgrade3.delivery_citations import replace_references_section


def test_english_plain_keywords_do_not_leak_into_abstract():
    text='# Example\n\n## Abstract\n\nBounded result.\n\nKeywords: optics; fixture\n\n## Results\n\nDetails.\n'
    assert _extract_front_matter(text)=={'title':'Example','abstract':'Bounded result.','keywords':['optics','fixture']}


def test_english_numbering_replaces_existing_reference_section_once():
    mapping={'references':[{'reference_number':1,'title':'Declared fixture','year':'2020','doi':''}]}
    text='# Example\n\n## Abstract\n\nBounded.\n\n## References\n\nold reference\n'
    numbered,replaced=replace_references_section(text,mapping,language='en')
    assert replaced and numbered.count('## References')==1
    assert '参考文献' not in numbered and '[1] Declared fixture (2020)' in numbered
    assert _verify_reader_draft(numbered,expected_refs=1)['all_pass']


def test_chinese_numbering_default_is_preserved():
    numbered,_=replace_references_section('# Example\n',{'references':[]})
    assert '## 参考文献' in numbered
