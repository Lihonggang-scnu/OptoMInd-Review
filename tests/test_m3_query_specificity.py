from optomind_research.gap_oa_expander import GapOAEvidenceExpander


def make_expander(*boost_terms: str, max_queries: int = 3) -> GapOAEvidenceExpander:
    return GapOAEvidenceExpander(
        max_queries=max_queries,
        query_boost_terms=list(boost_terms),
        real_llm_rerank=False,
        use_openalex=False,
        use_semantic_scholar=False,
        use_unpaywall=False,
    )


def test_upstream_planned_queries_are_preserved_for_role_targeted_retrieval():
    expander = make_expander(max_queries=2)
    queries = expander.build_queries(
        {
            "statement": "Broad chapter request that should not dilute the plan.",
            "planned_queries": [
                "quarter-wave stack optical thickness foundational theory",
                "dielectric multilayer filter historical development",
                "unused third query",
            ],
        },
        {"title": "Foundations"},
    )
    assert [row.query for row in queries] == [
        "quarter-wave stack optical thickness foundational theory",
        "dielectric multilayer filter historical development",
    ]
    assert expander.last_query_audit["mode"] == "upstream_planned_queries"


def test_generic_boost_phrases_do_not_replace_claim_specific_terms():
    expander = make_expander(
        "radiative cooling",
        "passive cooling",
        "daytime radiative cooling",
        "solar reflectance",
    )
    queries = expander.build_queries(
        {
            "statement": (
                "Achieving saturated colors through narrowband visible absorption reduces solar reflectance "
                "and cooling, creating an aesthetics trade-off."
            ),
            "evidence_type": "comparison",
        },
        {
            "title": "Optical appearance",
            "argument_role": "Navigating the Pareto front trade-off.",
            "_topic_context": "Radiative cooling and daytime radiative cooling are domain context.",
        },
    )

    assert len(queries) == 3
    token_lists = [query.query.lower().split() for query in queries]
    claim_specific = {
        "saturated", "colors", "narrowband", "visible", "absorption",
        "solar", "reflectance", "cooling", "aesthetics", "trade",
    }

    assert all(3 <= len(tokens) <= 7 for tokens in token_lists)
    assert any(len(tokens) < 7 for tokens in token_lists)
    assert all(len(set(tokens) & claim_specific) >= 2 for tokens in token_lists)
    assert claim_specific <= set().union(*(set(tokens) for tokens in token_lists))
    assert len({tuple(tokens[:3]) for tokens in token_lists}) == 3
    assert not {"achieving", "reduces", "creating", "navigating"} & set().union(
        *(set(tokens) for tokens in token_lists)
    )
    assert "solar reflectance" in queries[1].query.lower()
    assert "radiative cooling" in queries[2].query.lower()


def test_configured_bic_terms_are_retained_with_claim_core():
    expander = make_expander("bound states in the continuum", "quality factor")
    queries = expander.build_queries(
        {
            "statement": "Asymmetric dielectric resonators exhibit a tunable linewidth.",
            "evidence_type": "mechanism",
        },
        {
            "title": "BIC metasurfaces",
            "argument_role": "Explain resonance physics.",
            "_topic_context": "bound states in the continuum and quality factor are relevant.",
        },
    )

    joined = " ".join(query.query.lower() for query in queries)
    claim_specific = {"asymmetric", "dielectric", "resonators", "tunable", "linewidth"}
    assert claim_specific <= set(joined.split())
    assert "bound states in the continuum" in joined
    assert "quality factor" in joined
    assert all(3 <= len(query.query.split()) <= 7 for query in queries)
    assert all(len(set(query.query.lower().split()) & claim_specific) >= 2 for query in queries)


def test_long_topic_context_cannot_displace_claim_tokens_and_results_are_bounded():
    expander = make_expander("radiative cooling", max_queries=4)
    queries = expander.build_queries(
        {"statement": "Selective emission changes angular thermal response.", "evidence_type": "measurement"},
        {
            "title": "Long review context",
            "argument_role": "Measure the response.",
            "_topic_context": ("irrelevant contextword " * 100) + " radiative cooling",
        },
    )

    texts = [query.query.lower() for query in queries]
    claim_specific = {"selective", "emission", "angular", "thermal", "response"}
    assert len(texts) <= 4
    assert len(texts) == len(set(texts))
    assert claim_specific <= set(" ".join(texts).split())
    assert all(set(text.split()) & claim_specific for text in texts)
    assert all(len(text.split()) <= 7 for text in texts)
    assert all(text.split()[0] != "irrelevant" for text in texts)


def test_short_scientific_acronyms_survive_deterministic_query_fallback():
    expander = make_expander()
    queries = expander.build_queries(
        {
            "statement": (
                "Polarization splitting in high-index-contrast multilayer stacks at oblique "
                "incidence arises because TE and TM polarizations experience different "
                "effective refractive indices and phase accumulation."
            ),
            "evidence_type": "mechanism",
        },
        {
            "title": "Angular and polarization insensitivity",
            "argument_role": "Explain the physical origin.",
            "_topic_context": "All-dielectric optical multilayer filters.",
        },
    )

    joined = " ".join(row.query.lower() for row in queries)
    assert "te" in joined and "tm" in joined
    assert "multilayer" in joined
    assert "because" not in joined
