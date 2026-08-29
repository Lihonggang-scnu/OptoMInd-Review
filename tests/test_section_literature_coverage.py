from __future__ import annotations

from pathlib import Path

from optomind_research.full_review_production import audit_citations
from optomind_research.review_writer import (
    CitationBinder,
    OverclaimAuditor,
    SectionDraft,
    SectionMaterialPacket,
)
from optomind_research.section_literature_coverage import (
    COVERAGE_ROLES,
    SectionLiteratureCoverageExpander,
    coverage_candidate_chunks,
    filter_candidate_chunks_by_coverage_scope,
)
from optomind_research.section_coverage_oa_expander import (
    expand_section_coverage_gaps_oa,
)


def _section() -> dict:
    return {
        "section_id": "S03",
        "title": "Physical mechanisms and angular robustness",
        "argument_role": "Explain governing mechanisms and their boundary conditions.",
        "section_contract": {
            "central_thesis": "Angular robustness emerges from phase and dispersion control.",
            "argument_sequence": ["mechanism", "comparison", "boundary"],
            "paragraph_functions": ["explain", "compare", "synthesize"],
        },
    }


def test_coverage_planner_always_returns_six_role_decisions():
    plan = SectionLiteratureCoverageExpander(kb_path=None, real_llm=False).plan(_section())
    assert [row["role"] for row in plan["roles"]] == list(COVERAGE_ROLES)
    assert all(row["priority"] in {"required", "useful", "not_needed"} for row in plan["roles"])
    assert all(row["queries"] for row in plan["roles"] if row["priority"] != "not_needed")


def test_coverage_expander_builds_role_aware_source_landscape(monkeypatch, tmp_path: Path):
    kb = tmp_path / "review_knowledge_base.sqlite"
    kb.touch()

    def fake_query(_path, query, top_k=16, include_raw=True):
        lowered = query.lower()
        role = next((value for value, cue in {
            "application": "application deployment",
            "controversy": "controversy disagreement",
            "frontier": "recent frontier",
            "method": "experimental protocol",
            "foundation": "seminal origin",
            "mechanism": "physical mechanism",
        }.items() if cue in lowered), "mechanism")
        return {
            "text_chunks": [{
                "chunk_id": f"chunk-{role}-{index}",
                "paper_id": f"paper-{role}-{index}",
                "title": f"A {role} paper {index} on angular robustness",
                "section_path": "Results",
                "text_preview": f"This {role} literature explains angular robustness and optical design.",
            } for index in range(1, 4)]
        }

    monkeypatch.setattr("optomind_research.review_knowledge_base.query_kb", fake_query)
    monkeypatch.setattr(
        SectionLiteratureCoverageExpander,
        "_paper_metadata",
        lambda self, ids: {
            paper_id: {"paper_id": paper_id, "year": 2024, "venue": "Optics Journal"}
            for paper_id in ids
        },
    )
    coverage = SectionLiteratureCoverageExpander(kb_path=kb, real_llm=False).expand(_section())
    assert coverage["summary"]["paper_count"] == 18
    assert coverage["summary"]["uncovered_required_roles"] == []
    assert len(coverage_candidate_chunks(coverage)) == 18
    assert all(source["citation_policy"] == "chapter_context_and_synthesis" for source in coverage["sources"])


def test_contextual_source_can_support_review_synthesis_without_verbatim_entailment(monkeypatch):
    def no_llm(*_args, **_kwargs):
        raise AssertionError("A thematically relevant synthesis citation should not need sentence-level entailment.")

    monkeypatch.setattr("optomind_research.review_writer.call_qwen_chat", no_llm)
    packet = SectionMaterialPacket(
        section_id="S03",
        literature_coverage={
            "sources": [{
                "paper_id": "paper-mechanism",
                "title": "Mechanisms of angular robustness",
                "coverage_roles": ["mechanism"],
                "representative_chunks": [{
                    "chunk_id": "chunk-mechanism",
                    "text_preview": "Optical phase control enables angular robustness in multilayer structures.",
                }],
            }]
        },
    )
    draft = SectionDraft(
        "S03",
        english_text=(
            "Across these studies, angular robustness can be interpreted as a phase-control problem "
            "that links otherwise different design strategies [REF:paper-mechanism]."
        ),
    )
    bound = CitationBinder(real_llm=True).bind(draft, packet)
    assert bound.citation_map == {"0": ["chunk-mechanism"]}
    assert "[REF:paper-mechanism]" in bound.english_text


def test_contextual_source_cannot_launder_an_exact_measurement(monkeypatch):
    monkeypatch.setattr(
        "optomind_research.review_writer.call_qwen_chat",
        lambda *_args, **_kwargs: {
            "content": '{"supported":false,"support_type":"unsupported",'
            '"supported_clause":"","unsupported_clause":"exact measurement",'
            '"reason":"The passage does not report this value.","confidence":"high"}'
        },
    )
    packet = SectionMaterialPacket(
        section_id="S03",
        literature_coverage={
            "sources": [{
                "paper_id": "paper-context",
                "title": "Angular filters",
                "representative_chunks": [{
                    "chunk_id": "chunk-context",
                    "text_preview": "Angular filters use phase control.",
                }],
            }]
        },
    )
    draft = SectionDraft(
        "S03",
        english_text="The filter demonstrated a 0.2 nm linewidth [REF:paper-context].",
    )
    bound = CitationBinder(real_llm=True).bind(draft, packet)
    assert bound.citation_map == {}
    assert "[REF:paper-context]" not in bound.english_text


def test_external_oa_expansion_targets_chapter_role_not_a_fake_factual_claim(monkeypatch, tmp_path: Path):
    captured = {}

    def fake_expand(self, claim, section, **kwargs):
        captured.update({"claim": claim, "section": section, "kwargs": kwargs})
        return {
            "selected_oa_candidates": [],
            "backend_stats": {"openalex": {"raw": 3}},
            "candidate_stats": {"selected_candidates": 0},
            "download_summary": {"downloaded": 0},
        }

    monkeypatch.setattr(
        "optomind_research.section_coverage_oa_expander.GapOAEvidenceExpander.expand_claim",
        fake_expand,
    )
    kb = tmp_path / "review_knowledge_base.sqlite"
    kb.touch()
    blueprint = {
        "sections": [{
            **_section(),
            "literature_coverage": {
                "plan": SectionLiteratureCoverageExpander(kb_path=None, real_llm=False).plan(_section()),
                "sources": [],
                "coverage_gaps": [{
                    "role": "foundation",
                    "priority": "required",
                    "blocking": True,
                    "missing_papers": 2,
                    "coverage_question": "Which works established the governing problem?",
                    "intended_synthesis": "Explain how the field's central formulation emerged.",
                    "queries": ["angular robustness foundational theory"],
                }],
            },
        }]
    }
    _, report = expand_section_coverage_gaps_oa(
        blueprint,
        kb_sqlite=kb,
        output_dir=tmp_path / "oa",
        max_gaps=1,
        download_top_n=0,
    )
    assert report["status"] == "completed"
    assert captured["claim"]["claim_id"] == "S03-LIT-foundation"
    assert captured["claim"]["coverage_role"] == "foundation"
    assert "established" in captured["claim"]["statement"]


def test_explicit_scope_exclusion_rejects_keyword_relevant_platform():
    section = {
        **_section(),
        "scope_guardrails": ["Exclude metasurfaces and plasmonic approaches."],
    }
    agent = SectionLiteratureCoverageExpander(kb_path=None, real_llm=False)
    kept, audit = agent._audit_sources(
        section,
        agent.plan(section),
        [{
            "paper_id": "P-meta",
            "title": "All-dielectric metasurface for angular filtering",
            "coverage_roles": ["mechanism"],
            "representative_chunks": [{"text_preview": "Angular optical response."}],
        }],
    )
    assert kept == []
    assert audit["rejected_sources"] == 1
    assert audit["decisions"][0]["scope_fit"] == "out_of_scope"


def test_positive_assertion_refiner_removes_strawman_formula(monkeypatch):
    monkeypatch.setattr(
        "optomind_research.review_writer.call_qwen_chat",
        lambda *_args, **_kwargs: {
            "content": '{"replacements":[{"source":"The design is not merely compact but robust [REF:P1].",'
            '"replacement":"The design combines compactness with robustness [REF:P1]."}]}'
        },
    )
    draft = SectionDraft(
        "S01",
        english_text="The design is not merely compact but robust [REF:P1].",
    )
    refined = OverclaimAuditor(real_llm=True)._refine_strawman_contrasts(draft)
    assert "not merely" not in refined.english_text.lower()
    assert "[REF:P1]" in refined.english_text
    assert not [
        row for row in refined.overclaim_flags
        if row.get("overclaim_type") == "strawman_contrast_style"
    ]


def test_citation_audit_accepts_canonical_chapter_coverage_chunk(monkeypatch):
    monkeypatch.setattr(
        "optomind_research.full_review_production._judge_section_quality",
        lambda *_args, **_kwargs: {
            "verdict": "excellent",
            "unsupported_fact_detected": False,
            "scores": {},
        },
    )
    bundle = {
        "section_drafts": [{
            "section_id": "S01",
            "english_text": "The literature supports a phase-control framing [REF:P1].",
            "citation_map": {"0": ["chunk-P1"]},
            "status": "cited",
        }],
        "material_packets": [{
            "section_id": "S01",
            "section_contract": {"word_budget": 0, "paragraph_functions": []},
            "claims": [],
            "evidence_packets": [],
            "literature_coverage": {
                "sources": [{
                    "paper_id": "P1",
                    "representative_chunks": [{"chunk_id": "chunk-P1"}],
                }]
            },
        }],
    }
    audited = audit_citations(bundle, real_llm=True)
    row = audited["citation_audits"][0]
    assert row["invalid_cited_chunk_ids"] == []
    assert row["citation_ready"] is True


def test_scope_rejection_propagates_to_claim_candidate_pool():
    chunks = [
        {"chunk_id": "C1", "paper_id": "P-reject", "title": "Polariton filter"},
        {"chunk_id": "C2", "paper_id": "P-keep", "title": "Dielectric multilayer filter"},
        {"chunk_id": "C3", "paper_id": "P-other", "title": "Plasmonic angular filter"},
    ]
    coverage = {
        "source_scope_audit": {
            "explicit_excluded_terms": ["plasmonic"],
            "decisions": [{"paper_id": "P-reject", "keep": False}],
        }
    }
    kept, audit = filter_candidate_chunks_by_coverage_scope(chunks, coverage)
    assert [row["chunk_id"] for row in kept] == ["C2"]
    assert audit["rejected_chunks"] == 2


def test_excluded_term_in_body_comparison_does_not_misclassify_paper_identity():
    chunks = [{
        "chunk_id": "C1",
        "paper_id": "P1",
        "title": "All-dielectric multilayer angular filter",
        "text_preview": "Unlike plasmonic structures, this dielectric stack avoids metal loss.",
    }]
    coverage = {
        "source_scope_audit": {
            "explicit_excluded_terms": ["plasmonic"],
            "decisions": [],
        }
    }
    kept, audit = filter_candidate_chunks_by_coverage_scope(chunks, coverage)
    assert [row["chunk_id"] for row in kept] == ["C1"]
    assert audit["rejected_chunks"] == 0


def test_external_download_fails_closed_when_scope_audit_is_unreviewed(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setattr(
        "optomind_research.section_coverage_oa_expander.GapOAEvidenceExpander.expand_claim",
        lambda *_args, **_kwargs: {
            "selected_oa_candidates": [{
                "candidate_id": "doi:10.1/unreviewed",
                "title": "An apparently relevant paper",
                "doi": "10.1/unreviewed",
                "abstract": "Relevant-looking abstract.",
                "llm_scope_fit": "in_domain",
                "llm_retrieval_role": "evidence_candidate",
            }],
            "backend_stats": {},
            "candidate_stats": {},
        },
    )
    monkeypatch.setattr(
        "optomind_research.section_coverage_oa_expander.SectionLiteratureCoverageExpander.audit_external_candidates",
        lambda *_args, **_kwargs: {
            "decisions": [{
                "paper_id": "doi:10.1/unreviewed",
                "keep": True,
                "scope_fit": "unreviewed",
                "role_fit": ["foundation"],
                "decision_mode": "deterministic_fallback",
            }],
        },
    )
    kb = tmp_path / "review_knowledge_base.sqlite"
    kb.touch()
    blueprint = {
        "sections": [{
            **_section(),
            "literature_coverage": {
                "plan": SectionLiteratureCoverageExpander(
                    kb_path=None, real_llm=False
                ).plan(_section()),
                "coverage_gaps": [{
                    "role": "foundation",
                    "priority": "required",
                    "blocking": True,
                    "missing_papers": 1,
                    "coverage_question": "What founded this field?",
                    "intended_synthesis": "Establish the foundation.",
                    "queries": ["foundational query"],
                }],
            },
        }],
    }
    _, report = expand_section_coverage_gaps_oa(
        blueprint,
        kb_sqlite=kb,
        output_dir=tmp_path / "oa",
        max_gaps=1,
        download_top_n=1,
    )
    assert report["downloads_succeeded"] == 0
    assert report["records"][0]["status"] == "no_usable_candidate"
