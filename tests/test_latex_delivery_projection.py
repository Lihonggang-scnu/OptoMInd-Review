from optomind_research.runtime.latex_publication_renderer import (
    _clean_unicode, _render_main_tex, _strip_embedded_title_and_abstract,
    _wrap_plain_reference_dois_tex,
)


def test_owned_leading_title_requires_exact_confirmed_metadata_match():
    source = "\n<!-- manuscript-part:title:start -->\n\n# 当前题名\n\n<!-- manuscript-part:title:end -->\n\n## 引言\n入口\n\n# 正文知识章\n实质正文"
    projected = _strip_embedded_title_and_abstract(source, confirmed_title="当前题名")
    assert "# 当前题名" not in projected
    assert "# 正文知识章\n实质正文" in projected
    other = _strip_embedded_title_and_abstract(source, confirmed_title="另一题名")
    assert "# 当前题名" in other and "# 正文知识章" in other
    assert "# 当前题名" in source


def test_pending_author_is_diagnostic_metadata_but_never_rendered_placeholder():
    metadata = {"title": "Current title", "authors": [{"name": "Author information pending"}],
                "abstract": "Current abstract", "keywords": ["keyword"]}
    rendered = _render_main_tex(metadata=metadata, body_tex="Body", include_bibliography=False)
    assert r"\author{}" in rendered and "Author information pending" not in rendered
    assert metadata["authors"][0]["name"] == "Author information pending"
    metadata["authors"] = [{"name": "Actual Author", "affiliation": "University"}]
    rendered = _render_main_tex(metadata=metadata, body_tex="Body", include_bibliography=False)
    assert "Actual Author" in rendered and "University" in rendered


def test_numbered_reference_dois_wrap_without_changing_content_or_other_prose():
    doi = r"10.1200/jco.2026.44.16\_suppl.e20602"
    source = "DOI: " + doi + "\n" + r"{[}24{]} Title. DOI: " + doi + "\n"
    projected = _wrap_plain_reference_dois_tex(source)
    assert projected.startswith("DOI: " + doi + "\n")
    assert r"DOI: \url{10.1200/jco.2026.44.16_suppl.e20602}" in projected
    assert _wrap_plain_reference_dois_tex(projected) == projected
    assert _clean_unicode("1255\u2005Longitudinal") == "1255 Longitudinal"
