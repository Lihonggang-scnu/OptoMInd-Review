from __future__ import annotations

import json


def _claim(
    claim_id: str,
    section_id: str,
    statement: str,
    evidence_type: str,
    *,
    status: str = "direct",
    confidence: str = "high",
    components: list[str] | None = None,
    missing: list[str] | None = None,
) -> dict:
    return {
        "claim_id": claim_id,
        "section_id": section_id,
        "statement": statement,
        "evidence_type": evidence_type,
        "supporting_text_chunk_ids": [f"doi-10.0000-{claim_id}:hybrid:s0001"],
        "supporting_visual_chunk_ids": [],
        "saturation_score": 2.0,
        "evidence_binding_status": status,
        "evidence_binding_confidence": confidence,
        "evidence_binding_reason": "Only the listed components were verified against source text.",
        "evidence_component_map": [
            {"component": component, "chunk_ids": [f"doi-10.0000-{claim_id}:hybrid:s0001"]}
            for component in (components or [])
        ],
        "missing_evidence_components": missing or [],
        "evidence_spans": [
            {
                "chunk_id": f"doi-10.0000-{claim_id}:hybrid:s0001",
                "quote": (components or [statement])[0],
                "quote_translation": "",
            }
        ],
    }


def _section_meta() -> dict:
    return {
        "S01": {
            "title": "Mechanisms",
            "argument_role": "Explain the physical mechanism.",
            "text_chunk_map": {
                "doi-10.0000-S01-C01:hybrid:s0001": (
                    "Two-dimensional particles provide strong backscattering and low thermal resistance."
                )
            },
        },
        "S02": {
            "title": "Measurements",
            "argument_role": "Report measured optical performance.",
            "text_chunk_map": {
                "doi-10.0000-S02-C01:hybrid:s0001": (
                    "Measured solar reflectance increases when nanoparticle backscattering is introduced."
                )
            },
        },
    }


def test_claim_payload_carries_compact_evidence_grounding_fields():
    from optomind_research.argument_dag_builder import ArgumentDAGBuilder

    builder = ArgumentDAGBuilder(real_llm=False)
    payload = builder._build_claim_payload(
        _claim(
            "S01-C01",
            "S01",
            "Two-dimensional particles create optical backscattering but do not establish emissivity.",
            "mechanism",
            status="partial",
            confidence="medium",
            components=["two-dimensional particle backscattering", "low thermal resistance"],
            missing=["mid-infrared high emissivity", "wearable mid-infrared transparency"],
        ),
        _section_meta(),
    )

    assert payload["evidence_binding_status"] == "partial"
    assert payload["evidence_binding_confidence"] == "medium"
    assert "Only the listed components" in payload["evidence_binding_reason"]
    assert payload["evidence_component_map"] == [
        {
            "component": "two-dimensional particle backscattering",
            "chunk_ids": ["doi-10.0000-S01-C01:hybrid:s0001"],
        },
        {
            "component": "low thermal resistance",
            "chunk_ids": ["doi-10.0000-S01-C01:hybrid:s0001"],
        },
    ]
    assert payload["missing_evidence_components"] == [
        "mid-infrared high emissivity",
        "wearable mid-infrared transparency",
    ]
    assert payload["evidence_spans"][0]["quote"] == "two-dimensional particle backscattering"


def test_pairwise_review_rejects_missing_physical_bridge(monkeypatch):
    import llm.qwen_chat_client as chat_client
    from optomind_research.argument_dag_builder import ArgumentDAGBuilder

    calls: list[dict] = []

    def fake_chat(agent_name, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        calls.append({"agent": agent_name, "payload": payload})
        if agent_name == "DAGEdgeProposerAgent":
            return {
                "content": json.dumps(
                    {
                        "has_edge": True,
                        "relation_type": "contrasts_with",
                        "direction": "symmetric",
                        "confidence": "high",
                        "reason": (
                            "The source establishes mid-infrared high emissivity, which conflicts with "
                            "wearable mid-infrared transparency."
                        ),
                    }
                )
            }
        assert payload["claim_a"]["missing_evidence_components"] == ["mid-infrared high emissivity"]
        assert payload["claim_a"]["evidence_component_map"][0]["component"] == "two-dimensional particle backscattering"
        return {
            "content": json.dumps(
                {
                    "confirmed": False,
                    "relation_type": "contrasts_with",
                    "confidence": "low",
                    "reason": "Reject: the emissivity bridge is listed as missing evidence, not supported evidence.",
                }
            )
        }

    monkeypatch.setattr(chat_client, "call_qwen_chat", fake_chat)

    source = _claim(
        "S01-C01",
        "S01",
        "Two-dimensional particles provide backscattering and low thermal resistance.",
        "mechanism",
        status="partial",
        confidence="medium",
        components=["two-dimensional particle backscattering", "low thermal resistance"],
        missing=["mid-infrared high emissivity"],
    )
    target = _claim(
        "S02-C01",
        "S02",
        "Wearable fabrics can be transparent in the mid-infrared window.",
        "application",
        components=["wearable mid-infrared transparency"],
    )

    dag = ArgumentDAGBuilder(real_llm=True, global_critic=False, layer4_workers=1).build(
        [source, target],
        ["S01", "S02"],
        _section_meta(),
    )

    assert dag.edges == []
    assert dag.pruning_stats["critic_rejected"] == 1
    assert {call["agent"] for call in calls} == {"DAGEdgeProposerAgent", "DAGEdgeCriticAgent"}


def test_legitimate_mechanism_to_measurement_relation_is_retained(monkeypatch):
    import llm.qwen_chat_client as chat_client
    from optomind_research.argument_dag_builder import ArgumentDAGBuilder

    def fake_chat(agent_name, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        assert payload["claim_a"]["evidence_binding_status"] == "direct"
        assert payload["claim_a"]["missing_evidence_components"] == []
        if agent_name == "DAGEdgeProposerAgent":
            return {
                "content": json.dumps(
                    {
                        "has_edge": True,
                        "relation_type": "supports",
                        "direction": "A_to_B",
                        "confidence": "high",
                        "reason": (
                            "Nanoparticle backscattering in the source is the measured mechanism "
                            "behind higher solar reflectance in the target."
                        ),
                    }
                )
            }
        return {
            "content": json.dumps(
                {
                    "confirmed": True,
                    "relation_type": "supports",
                    "confidence": "high",
                    "reason": "The source mechanism is directly measured in the target claim.",
                }
            )
        }

    monkeypatch.setattr(chat_client, "call_qwen_chat", fake_chat)

    source = _claim(
        "S01-C01",
        "S01",
        "Nanoparticle backscattering increases solar reflectance.",
        "mechanism",
        components=["nanoparticle backscattering increases solar reflectance"],
    )
    target = _claim(
        "S02-C01",
        "S02",
        "Measured solar reflectance increases after nanoparticle backscattering is introduced.",
        "measurement",
        components=["measured solar reflectance increase"],
    )

    dag = ArgumentDAGBuilder(real_llm=True, global_critic=False, layer4_workers=1).build(
        [source, target],
        ["S01", "S02"],
        _section_meta(),
    )

    assert len(dag.edges) == 1
    assert dag.edges[0].relation_type == "supports"
    assert dag.edges[0].confidence == "high"


def test_cycle_detection_still_flags_backbone_cycles():
    from optomind_research.argument_dag_builder import ArgumentDAG, DAGEdge

    dag = ArgumentDAG(
        edges=[
            DAGEdge(
                edge_id="A->B",
                source_claim_id="A",
                target_claim_id="B",
                source_section_id="S01",
                target_section_id="S02",
                source_evidence_type="mechanism",
                target_evidence_type="mechanism",
                confidence="medium",
                relation_type="depends_on",
                is_dag_backbone=True,
            ),
            DAGEdge(
                edge_id="B->A",
                source_claim_id="B",
                target_claim_id="A",
                source_section_id="S02",
                target_section_id="S01",
                source_evidence_type="mechanism",
                target_evidence_type="mechanism",
                confidence="medium",
                relation_type="depends_on",
                is_dag_backbone=True,
            ),
        ]
    )

    assert "DAG contains a cycle (topological sort incomplete)" in dag.validate()


def test_same_section_explicit_boundary_is_preserved_as_qualifier():
    from optomind_research.argument_dag_builder import ArgumentDAGBuilder

    central = _claim(
        "S01-C01",
        "S01",
        "The mechanism increases the measured optical response.",
        "mechanism",
    )
    boundary = _claim(
        "S01-C02",
        "S01",
        "The response is limited under low-contrast operating conditions.",
        "mechanism",
        status="partial",
        components=["low-contrast operating conditions"],
    )
    boundary["section_fit"] = "boundary"

    dag = ArgumentDAGBuilder(real_llm=False, candidate_mode="exhaustive").build(
        [central, boundary],
        ["S01"],
        {"S01": {"title": "Mechanism and limits", "argument_role": "Bound the mechanism."}},
    )

    assert len(dag.edges) == 1
    assert dag.edges[0].source_claim_id == "S01-C01"
    assert dag.edges[0].target_claim_id == "S01-C02"
    assert dag.edges[0].relation_type == "qualifies"
    assert dag.edges[0].edge_readiness == "provisional"
    assert dag.pruning_stats["same_section_candidates"] == 1
    assert dag.pruning_stats["same_section_edges"] == 1


def test_same_section_contrast_is_non_backbone_and_not_dependency():
    from optomind_research.argument_dag_builder import ArgumentDAGBuilder

    left = _claim(
        "S01-C01",
        "S01",
        "Porous structures improve broadband reflectance.",
        "mechanism",
    )
    right = _claim(
        "S01-C02",
        "S01",
        "In contrast, dense structures improve narrowband reflectance.",
        "mechanism",
    )
    dag = ArgumentDAGBuilder(real_llm=False, candidate_mode="exhaustive").build(
        [left, right],
        ["S01"],
        {"S01": {"title": "Competing structures", "argument_role": "Compare routes."}},
    )

    assert len(dag.edges) == 1
    assert dag.edges[0].relation_type == "contrasts_with"
    assert dag.edges[0].is_dag_backbone is False
    assert dag.edges[0].edge_readiness == "grounded"


def test_global_critic_rejects_invented_source_target_pairs(monkeypatch):
    import llm.qwen_chat_client as chat_client
    from optomind_research.argument_dag_builder import ArgumentDAGBuilder, DAGEdge

    def fake_chat(agent_name, messages, **kwargs):
        assert agent_name == "DAGGlobalCriticAgent"
        payload = json.loads(messages[-1]["content"])
        assert len(payload["candidate_edges"]) == 1
        return {
            "content": json.dumps(
                {
                    "accepted_edges": [
                        {
                            "source_claim_id": "S01-C01",
                            "target_claim_id": "S02-C01",
                            "edge_id": "S01-C01->S02-C01",
                            "relation_type": "supports",
                            "confidence": "high",
                            "reason": "Keep the supplied pair.",
                        },
                        {
                            "source_claim_id": "S01-C02",
                            "target_claim_id": "S02-C01",
                            "edge_id": "S01-C02->S02-C01",
                            "relation_type": "supports",
                            "confidence": "medium",
                            "reason": "Invented source pair.",
                        },
                        {
                            "source_claim_id": "S01-C01",
                            "target_claim_id": "S02-C02",
                            "edge_id": "S01-C01->S02-C02",
                            "relation_type": "supports",
                            "confidence": "medium",
                            "reason": "Invented target pair.",
                        },
                        {
                            "source_claim_id": "S02-C01",
                            "target_claim_id": "S01-C01",
                            "edge_id": "S02-C01->S01-C01",
                            "relation_type": "supports",
                            "confidence": "medium",
                            "reason": "Invented reversed pair.",
                        },
                    ],
                    "graph_assessment": "One supplied edge plus three invented edges.",
                    "remaining_graph_risks": [],
                }
            )
        }

    monkeypatch.setattr(chat_client, "call_qwen_chat", fake_chat)
    claims = {
        "S01-C01": _claim("S01-C01", "S01", "A mechanism is grounded.", "mechanism", components=["grounded mechanism"]),
        "S01-C02": _claim("S01-C02", "S01", "A second mechanism is grounded.", "mechanism", components=["second mechanism"]),
        "S02-C01": _claim("S02-C01", "S02", "A measurement is grounded.", "measurement", components=["grounded measurement"]),
        "S02-C02": _claim("S02-C02", "S02", "A second measurement is grounded.", "measurement", components=["second measurement"]),
    }
    edge = DAGEdge(
        edge_id="S01-C01->S02-C01",
        source_claim_id="S01-C01",
        target_claim_id="S02-C01",
        source_section_id="S01",
        target_section_id="S02",
        source_evidence_type="mechanism",
        target_evidence_type="measurement",
        confidence="high",
        relation_type="supports",
        is_dag_backbone=True,
    )

    out, stats = ArgumentDAGBuilder(real_llm=True)._global_critique_edges(
        [edge],
        claims,
        ["S01", "S02"],
        _section_meta(),
    )

    assert [(e.source_claim_id, e.target_claim_id) for e in out] == [("S01-C01", "S02-C01")]
    assert len({(e.source_claim_id, e.target_claim_id) for e in out}) <= stats["before"]
    assert stats["before"] == 1
    assert stats["after"] == 1
    assert stats["invented_edges_rejected"] == 3


def test_global_critic_can_correct_relation_for_same_pair(monkeypatch):
    import llm.qwen_chat_client as chat_client
    from optomind_research.argument_dag_builder import ArgumentDAGBuilder, DAGEdge

    def fake_chat(agent_name, messages, **kwargs):
        assert agent_name == "DAGGlobalCriticAgent"
        return {
            "content": json.dumps(
                {
                    "accepted_edges": [
                        {
                            "source_claim_id": "S01-C01",
                            "target_claim_id": "S02-C01",
                            "edge_id": "S01-C01->S02-C01",
                            "relation_type": "supports",
                            "confidence": "medium",
                            "reason": "Same pair is corroborating rather than necessary.",
                        }
                    ],
                    "graph_assessment": "Corrected relation only.",
                    "remaining_graph_risks": [],
                }
            )
        }

    monkeypatch.setattr(chat_client, "call_qwen_chat", fake_chat)
    claims = {
        "S01-C01": _claim("S01-C01", "S01", "A mechanism is grounded.", "mechanism", components=["grounded mechanism"]),
        "S02-C01": _claim("S02-C01", "S02", "A measurement is grounded.", "measurement", components=["grounded measurement"]),
    }
    edge = DAGEdge(
        edge_id="S01-C01->S02-C01",
        source_claim_id="S01-C01",
        target_claim_id="S02-C01",
        source_section_id="S01",
        target_section_id="S02",
        source_evidence_type="mechanism",
        target_evidence_type="measurement",
        confidence="high",
        relation_type="depends_on",
        is_dag_backbone=True,
    )

    out, stats = ArgumentDAGBuilder(real_llm=True)._global_critique_edges(
        [edge],
        claims,
        ["S01", "S02"],
        _section_meta(),
    )

    assert len(out) == 1
    assert out[0].edge_id == "S01-C01->S02-C01"
    assert out[0].relation_type == "supports"
    assert stats["relation_corrections"] == 1
    assert stats["invented_edges_rejected"] == 0


def test_global_critic_uses_only_involved_claims_and_cannot_collapse_backbone(monkeypatch):
    import llm.qwen_chat_client as chat_client
    from optomind_research.argument_dag_builder import ArgumentDAGBuilder, DAGEdge

    captured = {}

    def fake_chat(agent_name, messages, **kwargs):
        assert agent_name == "DAGGlobalCriticAgent"
        payload = json.loads(messages[-1]["content"])
        captured.update(payload)
        first = payload["candidate_edges"][0]
        return {
            "content": json.dumps({
                "accepted_edges": [{
                    "source_claim_id": first["source_claim_id"],
                    "target_claim_id": first["target_claim_id"],
                    "edge_id": first["edge_id"],
                    "relation_type": "supports",
                    "confidence": "high",
                    "depends_on_missing_component": False,
                    "reason": "One retained relation.",
                }],
                "graph_assessment": "Over-pruned test output.",
                "remaining_graph_risks": [],
            })
        }

    monkeypatch.setattr(chat_client, "call_qwen_chat", fake_chat)
    claims = {}
    edges = []
    for index in range(10):
        source_id = f"S01-C{index:02d}"
        target_id = f"S02-C{index:02d}"
        claims[source_id] = _claim(source_id, "S01", f"Mechanism {index} is established.", "mechanism", components=[f"mechanism {index}"])
        claims[target_id] = _claim(target_id, "S02", f"Measurement {index} is established.", "measurement", components=[f"measurement {index}"])
        edges.append(DAGEdge(
            edge_id=f"{source_id}->{target_id}",
            source_claim_id=source_id,
            target_claim_id=target_id,
            source_section_id="S01",
            target_section_id="S02",
            source_evidence_type="mechanism",
            target_evidence_type="measurement",
            confidence="high",
            relation_type="supports",
            is_dag_backbone=True,
        ))
    claims["ORPHAN"] = _claim("ORPHAN", "S01", "Unrelated orphan claim.", "mechanism")

    out, stats = ArgumentDAGBuilder(real_llm=True)._global_critique_edges(
        edges, claims, ["S01", "S02"], _section_meta()
    )

    assert len(captured["claims"]) == 20
    assert all(row["claim_id"] != "ORPHAN" for row in captured["claims"])
    assert all("review_mentor_advice" not in row for row in captured["claims"])
    assert len(out) == 10
    assert stats["status"] == "rejected_global_output_overpruned"
    assert stats["proposed_after"] == 1
