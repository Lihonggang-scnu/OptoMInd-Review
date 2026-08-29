from __future__ import annotations

import json
import sqlite3
from pathlib import Path

def test_public_mainline_exports_resolve():
    import optomind_research as package

    for name in (
        "ReviewBlueprintPlanner",
        "ClaimDecomposer",
        "ArgumentDAGBuilder",
        "ReviewMentorAgent",
        "GapOAExpander",
        "VisualArgumentAlignment",
    ):
        assert getattr(package, name) is not None


def test_claim_parser_maps_short_refs_without_first_anchor_fallback():
    from optomind_research.claim_decomposer import _parse_llm_claims

    parsed = {
        "claims": [
            {
                "statement": "A spectrally selective structure suppresses an identified loss channel.",
                "evidence_type": "mechanism",
                "supporting_text_refs": ["T02"],
                "supporting_visual_refs": ["V01"],
                "saturation_score": 2.0,
                "load_bearing": True,
            },
            {
                "statement": "This deliberately unbound claim must remain visible as a gap.",
                "evidence_type": "comparison",
                "supporting_text_refs": ["T99"],
                "saturation_score": 2.0,
            },
        ]
    }
    claims = _parse_llm_claims(
        parsed,
        "S01",
        {"opaque:first", "opaque:second"},
        {"T01": "opaque:first", "T02": "opaque:second"},
        {"visual:one"},
        {"V01": "visual:one"},
    )
    assert claims[0].supporting_text_chunk_ids == ["opaque:second"]
    assert claims[0].supporting_visual_chunk_ids == ["visual:one"]
    assert claims[1].supporting_text_chunk_ids == []
    assert claims[1].saturation_score <= 0.5


def test_real_claim_path_runs_verifier_and_arbiter(monkeypatch, tmp_path: Path):
    import optomind_research.claim_decomposer as module
    from optomind_research.claim_evidence_verifier import ClaimEvidenceVerifier
    from optomind_research.evidence_arbiter import EvidenceTypeArbiter

    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Return JSON.", encoding="utf-8")

    def fake_chat(*args, **kwargs):
        return {
            "content": json.dumps(
                {
                    "claims": [
                        {
                            "statement": f"Supported scientific proposition number {i}.",
                            "evidence_type": "mechanism",
                            "supporting_text_refs": [f"T{i:02d}"],
                            "supporting_visual_refs": [],
                            "saturation_score": 1.0,
                            "load_bearing": i == 1,
                        }
                        for i in range(1, 4)
                    ]
                }
            )
        }

    def fake_verify(self, claims, section):
        for claim in claims:
            claim.evidence_binding_status = "direct"
            claim.evidence_binding_confidence = "high"
        return claims

    def fake_arbitrate(self, claims, section):
        for claim in claims:
            claim.evidence_type_confidence = "high"
        return claims

    monkeypatch.setattr(module, "call_qwen_chat", fake_chat)
    monkeypatch.setattr(ClaimEvidenceVerifier, "verify_and_bind", fake_verify)
    monkeypatch.setattr(EvidenceTypeArbiter, "arbitrate_section", fake_arbitrate)
    section = {
        "section_id": "S01",
        "title": "Mechanism",
        "argument_role": "Explain the governing physics.",
        "candidate_text_chunks": [
            {"chunk_id": f"chunk:{i}", "text_preview": f"Evidence text {i}."}
            for i in range(1, 4)
        ],
        "candidate_text_chunk_ids": [f"chunk:{i}" for i in range(1, 4)],
    }
    claims = module.ClaimDecomposer(prompt_path=prompt, real_llm=True).decompose_section(section)
    assert len(claims) == 3
    assert all(c.evidence_type_confidence == "high" for c in claims)


def test_m1_selection_is_category_balanced(tmp_path: Path):
    from optomind_research.review_mentor_agent import M1_CATEGORIES, ReviewMentorAgent

    library = {
        category: [
            {
                "move": f"{category} review organization",
                "transferable_rule": f"Rule for {category}",
                "trigger_when": "Use for review planning",
            }
        ]
        for category in M1_CATEGORIES
    }
    path = tmp_path / "moves.json"
    path.write_text(json.dumps(library), encoding="utf-8")
    agent = ReviewMentorAgent(
        active_library_path=path,
        use_vector_index=False,
        max_total_moves=len(M1_CATEGORIES),
        max_moves_per_category=1,
    )
    agent.load()
    selected = agent.select_moves("review planning")
    assert all(len(selected[category]) == 1 for category in M1_CATEGORIES)


def test_mentor_build_advice_exposes_command_knowledge_and_keeps_legacy_keys():
    from optomind_research.review_mentor_agent import ReviewMentorAgent

    agent = ReviewMentorAgent(real_llm=False)
    advice = agent.build_advice(
        user_question="How do mechanisms shape radiative cooling?",
        problem_understanding="Compare physical mechanisms.",
        scope_definition="Optical science.",
    )
    command_knowledge = advice["command_knowledge"]
    assert command_knowledge["status"] == "ok"
    assert {skill["name"] for skill in command_knowledge["skills"]} == {
        "top-review-architecture",
        "section-review-authoring",
        "global-review-audit",
        "manuscript-integration",
    }
    assert all(skill["digest"] for skill in command_knowledge["skills"])
    assert all(
        skill["provenance"]["source"]["commit"]
        for skill in command_knowledge["skills"]
    )
    assert advice["workflow_precedence"]["order"] == [
        "command_knowledge",
        "m1_case_moves",
    ]
    assert advice["workflow_precedence"]["does_not_rank_evidence"] is True
    assert advice["evidence_authority"]["exclusive"] is True
    assert (
        advice["evidence_authority"]["conflict_policy"]
        == "evidence_wins_or_claim_refused"
    )
    assert "cannot resolve scientific facts" in advice[
        "command_knowledge_boundary"
    ].lower()
    assert "guidance_precedence" not in advice
    assert advice["m1_case_moves"]["evidence_prohibition"] is True
    # Existing legacy consumers are unaffected.
    assert "usable_intellectual_moves" in advice
    assert "m2a_claim_decomposition_advice" in advice
    assert "selected_moves_digest" in advice


def test_planning_evidence_summary_reads_live_planner_keys():
    from optomind_research.review_mentor_agent import ReviewMentorAgent

    summary = ReviewMentorAgent._planning_evidence_summary(
        {
            "selected_concept_nodes": [{"label": "node"}],
            "retrieved_text_chunks": [{"chunk_id": "t"}],
            "retrieved_visual_chunks": [{"chunk_id": "v"}],
            "cluster_candidates": [{"cluster_id": "c"}],
        }
    )
    assert summary == {
        "concept_count": 1,
        "text_anchor_count": 1,
        "visual_anchor_count": 1,
        "cluster_count": 1,
        "top_labels": ["node"],
    }


def test_m3_ingest_updates_fts_and_preserves_existing_paper(monkeypatch, tmp_path: Path):
    import optomind_research.m3_kb_ingest as module

    db = tmp_path / "kb.sqlite"
    con = sqlite3.connect(db)
    module._ensure_text_chunks_table(con)
    con.execute(
        "INSERT INTO papers(paper_id,doi,title,year,venue,quality_tier,query_relevance,search_text,raw_json) VALUES(?,?,?,?,?,?,?,?,?)",
        ("doi:10.1/test", "10.1/test", "Rich title", 2020, "Venue", "core", "direct", "rich", "{}"),
    )
    con.commit()
    con.close()

    monkeypatch.setattr(
        module,
        "download_and_extract",
        lambda candidate, download_dir=None: (
            "Abstract\n\nThis optical mechanism paragraph contains enough structured scientific text for indexing. " * 3,
            "https://example.test/paper.pdf",
        ),
    )
    result = module.KBIngester(kb_sqlite=db).ingest_oa_candidates(
        [{"doi": "10.1/test", "title": "Poor replacement", "venue": ""}],
        {"claim_id": "S01-C01", "supporting_text_chunk_ids": [], "saturation_score": 0.0},
    )
    assert result.new_chunk_ids
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT title,quality_tier FROM papers WHERE paper_id='doi:10.1/test'").fetchone() == ("Rich title", "core")
        assert con.execute("SELECT count(*) FROM text_chunk_fts WHERE text_chunk_fts MATCH 'mechanism'").fetchone()[0] > 0
    finally:
        con.close()


def test_m3_scope_policy_quarantines_adjacent_cross_domain_evidence():
    from optomind_research.m3_kb_ingest import candidate_evidence_policy

    policy = candidate_evidence_policy({
        "llm_relevance_grade": "adjacent",
        "llm_support_status": "supports",
    })
    assert policy["scope_fit"] == "cross_domain_analogy"
    assert policy["retrieval_role"] == "method_transfer"
    assert policy["factual_support_allowed"] is False


def test_m3_strict_ingester_rejects_candidates_without_scope_audit(monkeypatch, tmp_path: Path):
    import optomind_research.m3_kb_ingest as module

    monkeypatch.setattr(
        module,
        "download_and_extract",
        lambda candidate, download_dir=None: ("Relevant optical evidence. " * 50, "local"),
    )
    result = module.KBIngester(
        kb_sqlite=tmp_path / "kb.sqlite",
        require_scope_audit=True,
    ).ingest_oa_candidates(
        [{"doi": "10.1/unaudited", "title": "Unaudited candidate"}],
        {"claim_id": "S01-C01", "supporting_text_chunk_ids": [], "saturation_score": 0.0},
    )
    assert result.new_chunk_ids == []
    assert result.stats["rejected_before_download"] == 1


def test_review_material_gate_does_not_promote_method_transfer_to_fact(tmp_path: Path):
    import json
    import optomind_research.m3_kb_ingest as ingest
    from optomind_research.review_writer import SectionMaterialMapper

    db = tmp_path / "review_knowledge_base.sqlite"
    con = sqlite3.connect(db)
    ingest._ensure_text_chunks_table(con)
    paper_raw = json.dumps({
        "llm_relevance_grade": "adjacent",
        "llm_support_status": "supports",
    })
    con.execute(
        "INSERT INTO papers(paper_id,doi,title,year,venue,quality_tier,query_relevance,search_text,raw_json) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("doi:10.1/analogy", "10.1/analogy", "Different-domain method", 2024, "Venue", "m3", "adjacent", "method", paper_raw),
    )
    con.execute(
        "INSERT INTO text_chunks(chunk_id,paper_id,doi,title,ordinal,section_path,char_start,char_end,char_count,"
        "boilerplate_score,text,search_text,raw_json,evidence_level,source_kind,provenance_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("m3gap:analogy:0000", "doi:10.1/analogy", "10.1/analogy", "Different-domain method", 0, "Results", 0, 60, 60, 0,
         "A transferable method was demonstrated in another application domain.", "transferable method", "{}", "fulltext", "fulltext", "{}"),
    )
    con.commit()
    con.close()
    packet = SectionMaterialMapper(db).map({
        "section_id": "S01",
        "claims": [{
            "claim_id": "S01-C01",
            "statement": "A target-domain effect is established.",
            "load_bearing": True,
            "evidence_requirement": "factual",
            "evidence_binding_status": "direct",
            "supporting_text_chunk_ids": ["m3gap:analogy:0000"],
        }],
    })
    assert packet.evidence_packets[0].retrieval_role == "method_transfer"
    assert packet.claims[0]["writing_permission"] == "evidence_gap_only"
    assert packet.uncited_load_bearing_claim_ids == ["S01-C01"]


def test_scientific_mojibake_repair_preserves_english_and_restores_symbols():
    from optomind_research.scientific_text_english_normalizer import repair_likely_scientific_mojibake

    source = "The reported mismatch is 螖n = 0.17 and the voltage-length product is Vpi路L."
    repaired = repair_likely_scientific_mojibake(source)
    assert "Δn = 0.17" in repaired
    assert "Vpi·L" in repaired


def test_scientific_mojibake_repair_handles_short_em_dash_sequences():
    from optomind_research.scientific_text_english_normalizer import repair_likely_scientific_mojibake

    source = (
        "Multidimensional information鈥攕patial, spectral, and angular鈥攊s "
        "captured in one acquisition."
    )
    repaired = repair_likely_scientific_mojibake(source)
    assert repaired == (
        "Multidimensional information—spatial, spectral, and angular—is "
        "captured in one acquisition."
    )


def test_figure_planner_preserves_verified_visual_when_llm_returns_empty(monkeypatch):
    import optomind_research.review_writer as module

    monkeypatch.setattr(module, "call_qwen_chat", lambda *args, **kwargs: {"content": '{"figure_placements": []}'})
    packet = module.SectionMaterialPacket(
        section_id="S01",
        evidence_packets=[module.EvidencePacket(
            claim_id="S01-C01",
            paper_id="p1",
            chunk_id="t1",
            exact_spans=["Evidence."],
            visual_refs=["v1"],
        )],
        visual_evidence=[{"chunk_id": "v1", "caption": "Measured comparison across conditions."}],
    )
    draft = module.FigurePlanner(real_llm=True).plan(
        module.SectionDraft(section_id="S01", english_text="A supported claim."), packet
    )
    assert draft.figure_placements[0]["visual_ref"] == "v1"
    assert draft.figure_placements[0]["source"] == "deterministic_verified_visual_fallback"
    assert draft.figure_placements[0]["needs_human_review"] is True


def test_supervisor_quality_score_ignores_unwritten_sections():
    from types import SimpleNamespace
    from run_review import _supervisor_quality_score

    class Suggestion:
        def __init__(self, row):
            self.row = row

        def to_dict(self):
            return self.row

    supervisor = SimpleNamespace(suggestions=[
        Suggestion({"target": "blueprint", "target_id": "S99", "severity": "critical"}),
        Suggestion({"target": "section_draft", "target_id": "S01", "severity": "medium"}),
        Suggestion({"target": "claims", "target_id": "S01-C01", "severity": "high"}),
    ])
    score, counts = _supervisor_quality_score(
        supervisor,
        section_ids={"S01"},
        claim_ids={"S01-C01"},
        include_global=False,
    )
    assert score == 110
    assert counts == {"critical": 0, "high": 1, "medium": 1, "low": 0}


def test_citation_semantic_cleanup_preserves_paragraphs_and_removes_unverified_marker(monkeypatch):
    import optomind_research.review_writer as module

    annotated = "Supported fact [REF:p1:c1].\n\nDifferent unsupported statement [REF:p1:c1]."
    def fake_call(agent_name, messages, **kwargs):
        sentence = json.loads(messages[1]["content"])["sentence"]
        supported = sentence.startswith("Supported fact")
        return {"content": json.dumps({
            "supported": supported,
            "support_type": "direct" if supported else "unsupported",
            "reason": "exact support" if supported else "different proposition",
            "confidence": "high",
        })}

    monkeypatch.setattr(module, "call_qwen_chat", fake_call)
    packet = module.SectionMaterialPacket(
        section_id="S01",
        claims=[{"claim_id": "c1", "statement": "Supported fact."}],
        evidence_packets=[module.EvidencePacket(
            claim_id="c1", paper_id="p1", chunk_id="chunk-1", exact_spans=["Supported fact."]
        )],
    )
    draft = module.CitationBinder(real_llm=True).bind(
        module.SectionDraft(section_id="S01", english_text=annotated), packet
    )
    assert "\n\n" in draft.english_text
    assert draft.english_text.count("[REF:p1:c1]") == 1
    assert any(flag.get("overclaim_type") == "citation_entailment_failure" for flag in draft.overclaim_flags)


def test_gap_queries_use_configured_domain_terms_not_hardcoded_topic():
    from optomind_research.gap_oa_expander import GapOAEvidenceExpander

    expander = GapOAEvidenceExpander(
        query_boost_terms=["bound states in the continuum", "quality factor"],
        real_llm_rerank=False,
        use_openalex=False,
        use_semantic_scholar=False,
        use_unpaywall=False,
    )
    queries = expander.build_queries(
        {"statement": "Bound states in the continuum can enhance quality factor.", "evidence_type": "mechanism"},
        {"title": "Dielectric metasurfaces", "argument_role": "Explain resonance physics."},
    )
    joined = " ".join(q.query.lower() for q in queries)
    assert "bound states in the continuum" in joined
    assert "radiative cooling" not in joined


def test_m3_reranker_requires_traceable_direct_evidence_and_normalizes_ten_point_scores(monkeypatch):
    import llm.qwen_chat_client as client
    from optomind_research.gap_oa_expander import GapOACandidate, GapOAEvidenceExpander

    candidates = [
        GapOACandidate(
            candidate_id="P1",
            title="Exact scaling result",
            abstract="The measured quality factor obeys an inverse square dependence on structural asymmetry.",
            relevance_score=0.5,
        ),
        GapOACandidate(
            candidate_id="P2",
            title="Related resonance platform",
            abstract="The platform supports high quality resonances for optical sensing applications.",
            relevance_score=0.5,
        ),
    ]

    def fake_chat(*args, **kwargs):
        return {
            "content": json.dumps(
                {
                    "rankings": [
                        {
                            "candidate_ref": "P01",
                            "grade": "direct",
                            "score": 9,
                            "confidence": "high",
                            "support_status": "supports",
                            "supported_clause": "inverse-square scaling",
                            "abstract_evidence_span": "quality factor obeys an inverse square dependence on structural asymmetry",
                            "likely_contribution": "Exact scaling evidence.",
                            "reason": "The relation is explicit.",
                        },
                        {
                            "candidate_ref": "P02",
                            "grade": "direct",
                            "score": 9,
                            "confidence": "high",
                            "support_status": "supports",
                            "supported_clause": "inverse-square scaling",
                            "abstract_evidence_span": "This invented quotation is absent from the supplied abstract",
                            "likely_contribution": "Topically related only.",
                            "reason": "Overclaimed direct support.",
                        },
                    ]
                }
            )
        }

    monkeypatch.setattr(client, "call_qwen_chat", fake_chat)
    expander = GapOAEvidenceExpander(
        real_llm_rerank=True,
        use_openalex=False,
        use_semantic_scholar=False,
        use_unpaywall=False,
    )
    report = expander._rerank_candidates(candidates, "quality factor follows inverse-square asymmetry scaling")
    assert report["status"] == "ok"
    assert candidates[0].llm_relevance_grade == "direct"
    assert candidates[0].llm_relevance_score == 0.9
    assert candidates[1].llm_relevance_grade == "adjacent"
    assert candidates[1].llm_relevance_score <= 0.74


def test_m3_reranker_quarantines_off_domain_keyword_overlap(monkeypatch):
    import llm.qwen_chat_client as client
    from optomind_research.gap_oa_expander import GapOACandidate, GapOAEvidenceExpander

    candidate = GapOACandidate(
        candidate_id="P1",
        title="Simulation-to-real transfer for air-conditioner fault diagnosis",
        abstract=(
            "A neural network trained on simulated air-conditioner faults transfers to real "
            "industrial sensor data."
        ),
        relevance_score=0.6,
    )

    def fake_chat(*args, **kwargs):
        return {
            "content": json.dumps({
                "rankings": [{
                    "candidate_ref": "P01",
                    "grade": "adjacent",
                    "score": 58,
                    "confidence": "high",
                    "support_status": "not_established",
                    "scope_fit": "off_domain",
                    "retrieval_role": "reject",
                    "supported_clause": "",
                    "abstract_evidence_span": "",
                    "likely_contribution": "Generic sim-to-real terminology only.",
                    "reason": "The application is unrelated to optical scattering imaging.",
                }]
            })
        }

    monkeypatch.setattr(client, "call_qwen_chat", fake_chat)
    report = GapOAEvidenceExpander(
        real_llm_rerank=True,
        use_openalex=False,
        use_semantic_scholar=False,
        use_unpaywall=False,
    )._rerank_candidates([candidate], "sim-to-real gap in optical scattering imaging")

    assert report["status"] == "ok"
    assert candidate.llm_scope_fit == "off_domain"
    assert candidate.llm_retrieval_role == "reject"
    assert candidate.llm_relevance_grade == "irrelevant"
    assert candidate.llm_relevance_score <= 0.24


def test_m3_reranker_does_not_download_adjacent_candidate_without_claim_component(monkeypatch):
    import llm.qwen_chat_client as client
    from optomind_research.gap_oa_expander import GapOACandidate, GapOAEvidenceExpander

    candidate = GapOACandidate(
        candidate_id="P1",
        title="Solar-powered interfacial water evaporation",
        abstract=(
            "A porous hydrogel improves solar absorption and accelerates water evaporation "
            "under one-sun illumination."
        ),
        relevance_score=0.6,
    )

    def fake_chat(*args, **kwargs):
        return {
            "content": json.dumps({
                "rankings": [{
                    "candidate_ref": "P01",
                    "grade": "adjacent",
                    "score": 62,
                    "confidence": "medium",
                    "support_status": "not_established",
                    "scope_fit": "in_domain",
                    "retrieval_role": "evidence_candidate",
                    "supported_clause": "",
                    "abstract_evidence_span": "",
                    "likely_contribution": "A neighboring solar-energy application.",
                    "reason": "It does not address angular emissivity in radiative cooling.",
                }]
            })
        }

    monkeypatch.setattr(client, "call_qwen_chat", fake_chat)
    report = GapOAEvidenceExpander(
        real_llm_rerank=True,
        use_openalex=False,
        use_semantic_scholar=False,
        use_unpaywall=False,
    )._rerank_candidates(
        [candidate],
        "angular dependence of thermal emissivity in passive radiative cooling",
    )

    assert report["status"] == "ok"
    assert candidate.llm_relevance_grade == "adjacent"
    assert candidate.llm_retrieval_role == "background_only"
    assert candidate.llm_support_status == "not_established"


def test_readiness_stops_specific_direct_claim_but_not_broad_consensus_claim():
    from optomind_research.evidence_readiness_gate import evaluate_evidence_readiness

    chunks = [
        {"paper_id": "p1", "publication_year": 2021, "text": "The model shows inverse-square scaling."},
        {"paper_id": "p2", "publication_year": 2023, "text": "Measurements confirm the scaling dependence."},
    ]
    specific = evaluate_evidence_readiness(
        claim_text="Quality factor follows inverse-square scaling with asymmetry.",
        supporting_chunks=chunks,
        claim_type="mechanism",
        binding_status="direct",
        binding_confidence="high",
        load_bearing=True,
    )
    broad = evaluate_evidence_readiness(
        claim_text="There is field-wide consensus that this law is universally dominant.",
        supporting_chunks=chunks,
        claim_type="mechanism",
        binding_status="direct",
        binding_confidence="high",
        load_bearing=True,
    )
    assert specific["action"] == "proceed"
    assert broad["action"] == "supplement"


def test_m3_ingester_reuses_existing_local_fulltext(monkeypatch, tmp_path: Path):
    import optomind_research.m3_kb_ingest as module

    html = (
        "<html><body><h1>Abstract</h1><p>Scientific abstract.</p>"
        "<h1>Introduction</h1><p>" + "optical mechanism evidence " * 150 + "</p>"
        "<h1>Results</h1><p>" + "measured result and discussion " * 100 + "</p>"
        "<h1>References</h1></body></html>"
    )
    local = tmp_path / "paper.html"
    local.write_text(html, encoding="utf-8")
    monkeypatch.setattr(module, "_try_download_bytes", lambda url: (_ for _ in ()).throw(AssertionError("network used")))
    text_value, source = module.download_and_extract({"local_download_path": str(local)})
    assert len(text_value) > 3000
    assert source == str(local)


def test_m3_oa_cli_loader_accepts_pipeline_stage_envelope(tmp_path: Path):
    from optomind_research.gap_oa_expander import load_claim_from_blueprint

    path = tmp_path / "11_gap_resolution.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "full_review.gap_resolution.v1",
                "blueprint": {
                    "input_context": {"user_question": "How does angle affect emissivity?"},
                    "sections": [
                        {
                            "section_id": "S01",
                            "claims": [
                                {
                                    "claim_id": "S01-C05",
                                    "statement": "Angular response remains insufficiently characterized.",
                                    "saturation_score": 0.5,
                                }
                            ],
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    section, claim = load_claim_from_blueprint(path, "S01-C05")
    assert section["section_id"] == "S01"
    assert claim["claim_id"] == "S01-C05"
    assert "How does angle affect emissivity?" in section["_topic_context"]


def test_scientific_translation_validation_preserves_critical_quantities():
    from optomind_research.scientific_text_english_normalizer import _validate_translation

    source = "\u6d4b\u91cf\u57281550 nm\u9644\u8fd1\u5f97\u5230Q=3.94\u00d710^4\uff0c\u89c1\u5f15\u6587[12]\u3002"
    translated = "The measurement near 1550 nm gives Q = 3.94 x 10^4, as reported in the cited source."
    assert _validate_translation(source, translated) == []


def test_staged_blueprint_grounding_keeps_binding_ids_local_and_promotes_real_gaps(monkeypatch, tmp_path: Path):
    import optomind_research.review_blueprint_planner as module

    planner = module.DynamicReviewBlueprintPlanner(
        concept_map_path=tmp_path / "concept.json",
        output_dir=tmp_path / "out",
        user_question="Question",
        problem_understanding="Problem",
        scope_definition="Scope",
    )
    architecture = {
        "review_thesis": "A falsifiable thesis.",
        "narrative_strategy": "A trade-off progression.",
        "sections": [
            {
                "section_id": "S01",
                "title": "Trade-off section",
                "argument_role": "Establish the governing trade-off.",
                "key_questions": ["Which boundary is fundamental?"],
                "claim_seeds": [{"claim_seed": "A candidate relation.", "relation_to_section": "support"}],
                "visual_argument_goals": [{"goal": "Show the boundary", "purpose": "Make the trade-off inspectable."}],
            }
        ],
    }
    evidence = {
        "selected_concept_nodes": [
            {"node_id": "n1", "label": "trade-off", "planning_value": "boundary"}
        ],
        "retrieved_text_chunks": [
            {"chunk_id": "t1", "title": "First", "section_path": "Results", "text_preview": "general trade-off"},
            {"chunk_id": "t2", "title": "Second", "section_path": "Results", "text_preview": "specific boundary"},
        ],
        "retrieved_visual_chunks": [
            {"chunk_id": "v1", "title": "First visual", "caption_preview": "overview", "visual_role": "schematic"},
            {"chunk_id": "v2", "title": "Second visual", "caption_preview": "boundary plot", "visual_role": "trend"},
        ],
    }

    def fake_chat(*args, **kwargs):
        return {
            "content": json.dumps(
                {
                    "section_id": "S01",
                    "concept_node_ids": ["n1"],
                    "text_chunk_ids": ["t1"],
                    "visual_chunk_ids": ["v1"],
                    "claim_bindings": [
                        {
                            "claim_seed": "A candidate relation.",
                            "supporting_text_chunk_ids": ["t2"],
                            "supporting_visual_chunk_ids": ["v2"],
                            "relation_to_section": "support",
                        }
                    ],
                    "visual_argument_slots": [
                        {"goal": "Show the boundary", "purpose": "Inspect it.", "visual_chunk_ids": ["v2"]}
                    ],
                    "uncovered_needs": ["Independent evidence for the boundary."],
                }
            )
        }

    monkeypatch.setattr(module, "call_qwen_chat", fake_chat)
    grounded = planner._ground_blueprint_architecture(architecture, evidence)
    section = grounded["sections"][0]
    assert set(section["claim_graph_seed"][0]["supporting_text_chunk_ids"]) <= set(section["text_chunk_ids"])
    assert set(section["claim_graph_seed"][0]["supporting_visual_chunk_ids"]) <= set(section["visual_chunk_ids"])
    assert grounded["high_value_gap_seeds"][0]["gap"] == "Independent evidence for the boundary."


def test_real_architect_order_is_not_forced_back_to_generic_physics_template(tmp_path: Path):
    from optomind_research.review_blueprint_planner import DynamicReviewBlueprintPlanner

    planner = DynamicReviewBlueprintPlanner(
        tmp_path / "concept.json",
        tmp_path / "out",
        user_question="Question",
        problem_understanding="Problem",
        scope_definition="Scope",
    )
    sections = [
        {"section_id": "S01", "title": "Deployment controversy", "argument_role": "Frame the disagreement."},
        {"section_id": "S02", "title": "Physical mechanism", "argument_role": "Resolve the disagreement."},
    ]
    preserved = planner._enforce_physics_first_order(sections, preserve_planner_order=True)
    assert [s["title"] for s in preserved] == ["Deployment controversy", "Physical mechanism"]
