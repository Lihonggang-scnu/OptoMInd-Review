from __future__ import annotations


def test_m3_zero_rounds_is_closure_only_and_performs_no_retrieval(tmp_path):
    from optomind_research.m3_real_gap_loop import run_m3_real_gap_loop

    blueprint = {
        "input_context": {"user_question": "A generic optical review question."},
        "sections": [
            {
                "section_id": "S01",
                "title": "Mechanism",
                "argument_role": "Explain a physical mechanism.",
                "claims": [
                    {
                        "claim_id": "S01-C01",
                        "statement": "A physical mechanism may limit device performance.",
                        "evidence_type": "mechanism",
                        "saturation_score": 0.5,
                        "supporting_text_chunk_ids": [],
                    }
                ],
            }
        ],
    }

    _, report = run_m3_real_gap_loop(
        blueprint,
        output_dir=tmp_path,
        max_rounds=0,
        adaptive_closure=False,
        use_openalex=False,
        use_semantic_scholar=False,
        use_unpaywall=False,
    )

    assert report["round_reports"] == []
    assert report["summary"]["candidate_packages"] == 0
    assert report["summary"]["stop_reason"] == "retrieval_skipped_by_configuration"
    assert not list(tmp_path.glob("round-*"))


def test_argument_dag_keeps_explicit_open_question_as_provisional_node():
    from optomind_research.argument_dag_builder import ArgumentDAGBuilder

    source = {
        "claim_id": "S01-C01",
        "section_id": "S01",
        "statement": "Waveguide loss constrains the attainable resonator quality factor.",
        "evidence_type": "mechanism",
        "evidence_requirement": "factual",
        "claim_state": "grounded",
        "evidence_binding_status": "direct",
        "supporting_text_chunk_ids": ["doi-10.0000-source:hybrid:s0001"],
        "missing_evidence_components": [],
        "saturation_score": 2.0,
    }
    target = {
        "claim_id": "S02-C01",
        "section_id": "S02",
        "statement": "Whether foundry-scale process variation preserves that quality factor remains open.",
        "evidence_type": "measurement",
        "evidence_requirement": "open_question",
        "claim_state": "open_question",
        "evidence_binding_status": "insufficient",
        "supporting_text_chunk_ids": [],
        "missing_evidence_components": ["foundry-scale process variation"],
        "saturation_score": 0.5,
    }

    dag = ArgumentDAGBuilder(real_llm=False).build(
        [source, target],
        ["S01", "S02"],
        {
            "S01": {"title": "Physical limits", "argument_role": "Establish the limit."},
            "S02": {"title": "Scale-up gap", "argument_role": "Identify unresolved scale-up evidence."},
        },
    )

    assert len(dag.edges) == 1
    assert dag.edges[0].edge_readiness == "provisional"
    assert dag.edges[0].requires_evidence_followup is True

