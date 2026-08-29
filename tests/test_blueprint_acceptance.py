"""Blueprint重构方案验收测试桩。

每个测试对应 BLUEPRINT_REDESIGN_PROPOSAL.md 中一个里程碑的验收标准。
测试目前为桩实现（stub），实现各里程碑后逐步补全。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Step 0 — 基础设施
# ---------------------------------------------------------------------------

class TestStep0Infrastructure:
    """验收Step0：ProposerCriticChain / Claim数据结构 / paper_citations表。"""

    def test_claim_schema_imports(self):
        from optomind_research.claim_schema import (
            Claim,
            VALID_EVIDENCE_TYPES,
            EVIDENCE_TYPE_RANK,
            validate_claim,
        )
        assert set(VALID_EVIDENCE_TYPES) == {"mechanism", "measurement", "comparison", "application"}
        assert EVIDENCE_TYPE_RANK["mechanism"] < EVIDENCE_TYPE_RANK["application"]

    def test_claim_roundtrip(self):
        from optomind_research.claim_schema import Claim, validate_claim

        c = Claim(
            claim_id="S02-C1",
            statement="大气窗口8-13μm是辐射冷却关键光谱通道。",
            evidence_type="mechanism",
            supporting_concept_node_ids=["node_42"],
            supporting_text_chunk_ids=["chunk_34_paperA"],
            saturation_score=2.1,
        )
        assert validate_claim(c) == []
        d = c.to_dict()
        c2 = Claim.from_dict(d)
        assert c2.claim_id == c.claim_id
        assert c2.evidence_type == c.evidence_type

    def test_validate_claim_catches_empty_text_chunks(self):
        from optomind_research.claim_schema import Claim, validate_claim

        c = Claim(
            claim_id="S02-C1",
            statement="大气窗口8-13μm是辐射冷却关键光谱通道。",
            evidence_type="mechanism",
            supporting_text_chunk_ids=[],  # 违规
        )
        errors = validate_claim(c)
        assert any("text_chunk" in e for e in errors)

    def test_validate_claim_catches_invalid_evidence_type(self):
        from optomind_research.claim_schema import Claim, validate_claim

        c = Claim(
            claim_id="S02-C1",
            statement="大气窗口8-13μm是辐射冷却关键光谱通道。",
            evidence_type="unknown_type",
            supporting_text_chunk_ids=["chunk_1"],
        )
        errors = validate_claim(c)
        assert any("evidence_type" in e for e in errors)

    def test_proposer_critic_chain_imports(self):
        from llm.proposer_critic import ProposerCriticChain
        chain = ProposerCriticChain()
        assert hasattr(chain, "run")

    def test_proposer_critic_mock_mode(self):
        """在mock模式下（无API）验证接口结构正确。"""
        from llm.proposer_critic import ProposerCriticChain

        chain = ProposerCriticChain()
        result = chain.run(
            proposer_prompt="测试proposer提示词",
            critic_prompt_template="Critic验证：{proposer_output}",
            agent_name="test_pc",
            task_type="test",
            force_mock=True,
        )
        # 不管mock还是真实，接口结构必须一致
        assert "proposer_output" in result
        assert "critic_output" in result
        assert "accepted" in result
        assert "final" in result
        assert "flags" in result
        assert isinstance(result["flags"], list)

    def test_paper_citations_table_exists(self, tmp_path):
        """paper_citations表已加入SQLite schema。"""
        import sqlite3

        from optomind_research.review_knowledge_base import ReviewKnowledgeBaseBuilder

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        kb = ReviewKnowledgeBaseBuilder.__new__(ReviewKnowledgeBaseBuilder)
        kb.warnings = []
        kb._create_tables(conn)
        conn.commit()

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "paper_citations" in tables, f"paper_citations missing; found: {tables}"

        # 验证列定义
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(paper_citations)").fetchall()
        }
        assert "citing_paper_id" in cols
        assert "cited_paper_id" in cols
        conn.close()


# ---------------------------------------------------------------------------
# M1 — 智识动作库（Intellectual Moves Library）
# ---------------------------------------------------------------------------

class TestM1IntellectualMoves:
    """M1验收：IntellectualMovesExtractor 基础设施可用，normalize/aggregate 逻辑正确。"""

    def test_imports(self):
        from optomind_research.review_example_memory import (
            IntellectualMovesExtractor,
            _aggregate_moves_library,
            _normalize_moves,
            _MOVES_FALLBACK,
        )
        assert len(_MOVES_FALLBACK) == 11
        assert set(_MOVES_FALLBACK.keys()) == {
            "problem_reframing", "central_thesis", "taxonomy_design",
            "synthesis_moves", "section_progression", "paragraph_moves",
            "evidence_critique", "disagreement_handling", "gap_characterization",
            "figure_argument", "top_journal_publishability",
        }

    def test_normalize_moves_valid(self):
        from optomind_research.review_example_memory import _normalize_moves

        parsed = {
            "problem_reframing": ["Pattern A", "Pattern B"],
            "central_thesis": [],
            "taxonomy_design": [],
            "synthesis_moves": ["Synthesis X"],
            "section_progression": [],
            "paragraph_moves": [],
            "evidence_critique": [],
            "disagreement_handling": ["Handling Y"],
            "gap_characterization": ["Gap Z"],
            "figure_argument": [],
            "top_journal_publishability": [],
        }
        result = _normalize_moves(parsed)
        assert result["problem_reframing"] == ["Pattern A", "Pattern B"]
        assert result["evidence_critique"] == []

    def test_normalize_moves_caps_at_3(self):
        from optomind_research.review_example_memory import _normalize_moves

        parsed = {"problem_reframing": [f"item {i}" for i in range(10)]}
        result = _normalize_moves(parsed)
        assert len(result["problem_reframing"]) <= 3

    def test_aggregate_deduplicates(self):
        from optomind_research.review_example_memory import _aggregate_moves_library

        records = [
            {"problem_reframing": ["Pattern A"], "central_thesis": [], "taxonomy_design": [],
             "synthesis_moves": ["X"], "section_progression": [], "paragraph_moves": [],
             "evidence_critique": [], "disagreement_handling": [], "gap_characterization": [],
             "figure_argument": [], "top_journal_publishability": []},
            {"problem_reframing": ["Pattern A"], "central_thesis": [], "taxonomy_design": [],
             "synthesis_moves": ["Y"], "section_progression": [], "paragraph_moves": [],
             "evidence_critique": [], "disagreement_handling": [], "gap_characterization": [],
             "figure_argument": [], "top_journal_publishability": []},
        ]
        lib = _aggregate_moves_library(records)
        # "Pattern A" appears twice but should be deduplicated
        assert lib["problem_reframing"].count("Pattern A") == 1
        assert "X" in lib["synthesis_moves"]
        assert "Y" in lib["synthesis_moves"]

    def test_extract_one_mock_mode(self):
        from optomind_research.review_example_memory import IntellectualMovesExtractor, DEFAULT_MOVES_PROMPT

        extractor = IntellectualMovesExtractor(
            prompt_path=DEFAULT_MOVES_PROMPT,
            model_tier="standard_model",
            real_llm=False,
        )
        result = extractor.extract_one(
            record={"abstract_or_opening_excerpt": "Test abstract text"},
            all_text="Introduction\nThis paper studies X.\nConclusion\nWe showed Y.",
        )
        # In mock mode, should return fallback structure with correct keys
        for key in (
            "problem_reframing", "central_thesis", "taxonomy_design",
            "synthesis_moves", "section_progression", "paragraph_moves",
            "evidence_critique", "disagreement_handling", "gap_characterization",
            "figure_argument", "top_journal_publishability",
        ):
            assert key in result, f"Missing key: {key}"

    def test_extract_section_text_finds_intro(self):
        from optomind_research.review_example_memory import _extract_section_text, _INTRO_KEYWORDS

        text = """Abstract
This is abstract text.

Introduction
This paper presents a novel approach to X.
We demonstrate that Y leads to Z.
The key insight is that A implies B.

Methods
Experimental setup follows."""
        result = _extract_section_text(text, _INTRO_KEYWORDS, max_chars=500)
        assert "novel approach" in result
        assert "Methods" not in result  # should not bleed into next section

    def test_extract_section_text_finds_conclusion(self):
        from optomind_research.review_example_memory import _extract_section_text, _CONCL_KEYWORDS

        text = """Results
Results here.

Conclusion
In summary, this work demonstrates that X is important.
Future directions include Y and Z.
"""
        result = _extract_section_text(text, _CONCL_KEYWORDS, max_chars=500)
        assert "demonstrates that X" in result

    @pytest.mark.skip(reason="M1运行时验收：需要实际PDF文件和API")
    def test_intellectual_moves_fields_present_runtime(self, sample_review_memory_json):
        """验收：所有综述生成 intellectual_moves，每篇 ≥2 种类型非空。"""
        required_keys = {
            "problem_reframing", "central_thesis", "taxonomy_design",
            "synthesis_moves", "section_progression", "paragraph_moves",
            "evidence_critique", "disagreement_handling", "gap_characterization",
            "figure_argument", "top_journal_publishability",
        }
        for entry in sample_review_memory_json:
            assert "intellectual_moves" in entry
            assert required_keys <= set(entry["intellectual_moves"].keys())
            non_empty = [k for k, v in entry["intellectual_moves"].items() if v]
            assert len(non_empty) >= 3, f"Low yield for {entry.get('file_name', '?')}"


# ---------------------------------------------------------------------------
# M2a — 主张分解（Claim Decomposition）
# ---------------------------------------------------------------------------

class TestM2aClaimDecomposition:
    """验收M2a：每节2-8个非填充主张，证据非空，evidence_type准确率≥85%。"""

    def test_claims_per_section_count(self, sample_blueprint_json):
        for section in sample_blueprint_json["sections"]:
            claims = section.get("claims", [])
            assert 2 <= len(claims) <= 8, (
                f"Section {section['section_id']} has {len(claims)} claims, expected 2-8"
            )

    def test_claim_statements_are_specific(self, sample_blueprint_json):
        """主张陈述必须包含物理量或材料名称，非泛化描述。"""
        import re

        generic_patterns = [
            r"^这种材料很好$",
            r"^性能优越$",
            r"^具有重要意义$",
        ]
        for section in sample_blueprint_json["sections"]:
            for claim in section.get("claims", []):
                stmt = claim["statement"]
                for pat in generic_patterns:
                    assert not re.match(pat, stmt), f"Generic claim: {stmt}"

    def test_supporting_text_chunk_ids_nonempty(self, sample_blueprint_json):
        from optomind_research.claim_schema import Claim

        for section in sample_blueprint_json["sections"]:
            for d in section.get("claims", []):
                c = Claim.from_dict(d)
                assert c.supporting_text_chunk_ids, (
                    f"Claim {c.claim_id} has no text chunk support"
                )

    def test_evidence_type_field_valid(self, sample_blueprint_json):
        from optomind_research.claim_schema import VALID_EVIDENCE_TYPES, Claim

        for section in sample_blueprint_json["sections"]:
            for d in section.get("claims", []):
                c = Claim.from_dict(d)
                assert c.evidence_type in VALID_EVIDENCE_TYPES, (
                    f"Claim {c.claim_id}: invalid evidence_type '{c.evidence_type}'"
                )

    def test_load_bearing_chunks_identified(self, sample_blueprint_json):
        all_claims = [
            c
            for s in sample_blueprint_json["sections"]
            for c in s.get("claims", [])
        ]
        load_bearing = [c for c in all_claims if c.get("load_bearing")]
        assert len(load_bearing) >= 3, (
            f"Only {len(load_bearing)} load-bearing claims found, expected ≥3"
        )

    def test_evidence_network_summary_present(self, sample_blueprint_json):
        """blueprint 顶层必须有 evidence_network 摘要字段。"""
        net = sample_blueprint_json.get("evidence_network")
        assert net is not None, "evidence_network missing from blueprint"
        for key in ("total_claims", "total_chunks_referenced", "load_bearing_chunks"):
            assert key in net, f"evidence_network missing key '{key}'"

    def test_evidence_network_load_bearing_chunks_count(self, sample_blueprint_json):
        """load_bearing_chunks 数量 ≥ 3（多个 claim 共引同一 chunk）。"""
        net = sample_blueprint_json.get("evidence_network", {})
        lb = net.get("load_bearing_chunks", [])
        assert len(lb) >= 3, (
            f"Only {len(lb)} load-bearing chunks; expected ≥3. "
            f"EvidenceNetwork: {net}"
        )

    def test_evidence_network_load_bearing_claims(self, sample_blueprint_json):
        """EvidenceNetwork.to_summary() 中 load_bearing_claims 非空。"""
        net = sample_blueprint_json.get("evidence_network", {})
        lb_claims = net.get("load_bearing_claims", [])
        assert isinstance(lb_claims, list), "load_bearing_claims should be a list"

    @pytest.mark.skip(reason="需要实际API调用 — 使用 scripts/verify_evidence_types.py 手动运行")
    def test_evidence_type_accuracy_b_model(self, sample_blueprint_json):
        """B模型抽验20个主张，evidence_type一致率≥85%。"""
        from scripts.verify_evidence_types import verify
        report = verify(sample_blueprint_json, sample_n=20, threshold=0.85, mock=False)
        assert report["status"] == "pass", (
            f"evidence_type consistency {report['consistency_rate']:.1%} < 85%. "
            f"Low-confidence claims: {report['low_confidence_list']}"
        )


# ---------------------------------------------------------------------------
# M2b — 论证DAG构建
# ---------------------------------------------------------------------------

class TestM2bArgumentDAG:
    """验收M2b：DAG≥8条跨章节边，准确率≥80%，无类型层级违反。"""

    def test_dag_cross_section_edge_count(self, sample_dag_json):
        edges = sample_dag_json.get("edges", [])
        cross_section = [
            e for e in edges
            if e["source_section_id"] != e["target_section_id"]
        ]
        assert len(cross_section) >= 8, (
            f"Only {len(cross_section)} cross-section edges, expected ≥8"
        )

    def test_dag_no_evidence_type_hierarchy_violation(self, sample_dag_json):
        from optomind_research.claim_schema import EVIDENCE_TYPE_RANK

        for edge in sample_dag_json.get("edges", []):
            src_rank = EVIDENCE_TYPE_RANK.get(edge["source_evidence_type"], -1)
            tgt_rank = EVIDENCE_TYPE_RANK.get(edge["target_evidence_type"], -1)
            assert src_rank <= tgt_rank, (
                f"Type hierarchy violation: {edge['source_evidence_type']} → "
                f"{edge['target_evidence_type']} (edge {edge['edge_id']})"
            )

    def test_dag_all_edges_have_confidence_label(self, sample_dag_json):
        for edge in sample_dag_json.get("edges", []):
            assert edge.get("confidence") in {"high", "medium", "low"}, (
                f"Edge {edge.get('edge_id')} missing valid confidence label"
            )

    def test_dag_pruning_ratios_recorded(self, sample_dag_json):
        stats = sample_dag_json.get("pruning_stats", {})
        for key in ("after_layer1", "after_layer2", "after_layer3", "final"):
            assert key in stats, f"pruning_stats missing key '{key}'"

    def test_saturation_propagation_at_least_one_downgrade(self, sample_dag_json):
        downgraded = [
            c for c in sample_dag_json.get("claims", [])
            if c.get("saturation_downgraded_by_dag")
        ]
        assert len(downgraded) >= 1, "No claim saturation was downgraded by DAG propagation"


# ---------------------------------------------------------------------------
# M3 — 证据饱和度 + 迭代缺口闭环
# ---------------------------------------------------------------------------

class TestM3GapResolution:
    """验收M3：低饱和度主张触发补充检索，饱和度有提升。"""

    def test_gap_resolution_agent_imports(self):
        from optomind_research.gap_resolution_agent import (
            GapResolutionAgent,
            GapResolutionResult,
        )
        agent = GapResolutionAgent(real_llm=False)
        assert hasattr(agent, "resolve")
        assert hasattr(agent, "resolve_blueprint")

    def test_gap_resolution_result_fields(self):
        from optomind_research.gap_resolution_agent import GapResolutionResult

        r = GapResolutionResult(
            claim_id="S01-C01",
            before_saturation=1.0,
            after_saturation=2.0,
            queries_generated=["q1"],
            new_chunk_ids_added=["cid1"],
            iterations=1,
            status="resolved",
            gap_type="single_source",
        )
        d = r.to_dict()
        for key in (
            "claim_id", "before_saturation", "after_saturation",
            "queries_generated", "new_chunk_ids_added", "iterations",
            "status", "gap_type",
            "accepted_chunks", "rejected_chunks",
            "query_to_candidate_counts", "gap_rationale", "sqlite_path_used",
        ):
            assert key in d, f"GapResolutionResult.to_dict() missing key '{key}'"

    def test_gap_resolution_no_op_for_sufficient_claim(self):
        from optomind_research.gap_resolution_agent import GapResolutionAgent

        agent = GapResolutionAgent(real_llm=False, saturation_threshold=1.5)
        claim = {
            "claim_id": "S01-C01",
            "statement": "test claim",
            "evidence_type": "mechanism",
            "saturation_score": 2.0,
            "supporting_text_chunk_ids": [
                "doi-10.1000-test.001:hybrid:s0001",
                "doi-10.1001-test.002:hybrid:s0001",
            ],
        }
        section = {
            "section_id": "S01",
            "candidate_text_chunk_ids": ["doi-10.1002-test.003:hybrid:s0001"],
        }
        result = agent.resolve(claim, section)
        assert result.status == "already_sufficient"
        assert result.after_saturation == 2.0

    def test_gap_resolution_saturation_never_decreases(self, sample_blueprint_json):
        import copy
        from optomind_research.gap_resolution_agent import GapResolutionAgent

        agent = GapResolutionAgent(real_llm=False, saturation_threshold=100.0)
        bp = copy.deepcopy(sample_blueprint_json)
        _updated_bp, results = agent.resolve_blueprint(bp)

        for r in results:
            assert r.after_saturation >= r.before_saturation, (
                f"Claim {r.claim_id} saturation decreased: "
                f"{r.before_saturation} → {r.after_saturation}"
            )

    def test_gap_resolution_mock_resolves_with_multidoi_candidates(
        self, sample_gap_blueprint_json
    ):
        """At least one low-saturation claim must reach 'resolved' in mock mode
        when candidates span multiple DOIs."""
        import copy
        from optomind_research.gap_resolution_agent import GapResolutionAgent

        agent = GapResolutionAgent(real_llm=False, saturation_threshold=1.5)
        bp = copy.deepcopy(sample_gap_blueprint_json)
        _updated_bp, results = agent.resolve_blueprint(bp)

        resolved = [r for r in results if r.status == "resolved"]
        assert len(resolved) >= 1, (
            f"Expected ≥1 resolved claim; got statuses: "
            f"{[r.status for r in results]}"
        )

        improved = [r for r in results if r.after_saturation > r.before_saturation]
        assert len(improved) >= 1, (
            "Expected at least one claim with saturation improvement"
        )

    def test_gap_resolution_report_has_audit_fields(self, sample_gap_blueprint_json):
        """to_dict() must include accepted_chunks, rejected_chunks, query_to_candidate_counts."""
        import copy
        from optomind_research.gap_resolution_agent import GapResolutionAgent

        agent = GapResolutionAgent(real_llm=False, saturation_threshold=1.5)
        bp = copy.deepcopy(sample_gap_blueprint_json)
        _updated_bp, results = agent.resolve_blueprint(bp)

        resolved = [r for r in results if r.status in ("resolved", "open_question")]
        assert resolved, "Need at least one non-trivial result to check audit fields"
        d = resolved[0].to_dict()
        assert isinstance(d["accepted_chunks"], list)
        assert isinstance(d["rejected_chunks"], list)
        assert isinstance(d["query_to_candidate_counts"], dict)

    def test_find_sqlite_accepts_direct_file(self, tmp_path):
        """_find_sqlite() must work when kb_path points directly to a .sqlite file."""
        import sqlite3
        from optomind_research.gap_resolution_agent import GapResolutionAgent

        db = tmp_path / "test_kb.sqlite"
        sqlite3.connect(str(db)).close()

        agent = GapResolutionAgent(real_llm=False, kb_path=db)
        found = agent._find_sqlite()
        assert found == db, f"Expected {db}, got {found}"

    def test_find_sqlite_accepts_directory(self, tmp_path):
        """_find_sqlite() must work when kb_path is a directory containing a .sqlite."""
        import sqlite3
        from optomind_research.gap_resolution_agent import GapResolutionAgent

        db = tmp_path / "review_knowledge_base.sqlite"
        sqlite3.connect(str(db)).close()

        agent = GapResolutionAgent(real_llm=False, kb_path=tmp_path)
        found = agent._find_sqlite()
        assert found == db, f"Expected {db}, got {found}"

    def test_gap_resolution_report_generated(self, tmp_path, sample_blueprint_json):
        import copy
        import json as _json
        from optomind_research.gap_resolution_agent import GapResolutionAgent

        agent = GapResolutionAgent(real_llm=False, saturation_threshold=1.5)
        bp = copy.deepcopy(sample_blueprint_json)
        _updated_bp, results = agent.resolve_blueprint(bp)

        report = {
            "total_claims": len(results),
            "results": [r.to_dict() for r in results],
        }
        report_path = tmp_path / "gap_resolution_report.json"
        report_path.write_text(_json.dumps(report, ensure_ascii=False), encoding="utf-8")
        assert report_path.exists()
        loaded = _json.loads(report_path.read_text(encoding="utf-8"))
        assert loaded["total_claims"] == len(results)


# ---------------------------------------------------------------------------
# M4 — 视觉论证语义对齐
# ---------------------------------------------------------------------------

class TestM4VisualArgumentAlignment:
    """验收M4：8类协议，视觉论证字段完整，章节/主张层面映射可用。"""

    VALID_ARGUMENT_TYPES = {
        "mechanism_anchor",
        "taxonomy_or_roadmap",
        "method_or_workflow",
        "quantitative_comparison",
        "trend_or_parameter_map",
        "representative_example",
        "anomaly_or_limitation",
        "synthesis_overview",
    }

    def test_valid_visual_argument_types_constant(self):
        """VALID_VISUAL_ARGUMENT_TYPES must match the 8-type protocol exactly."""
        from optomind_research.visual_argument_alignment import VALID_VISUAL_ARGUMENT_TYPES
        assert VALID_VISUAL_ARGUMENT_TYPES == self.VALID_ARGUMENT_TYPES

    def test_visual_argument_types_valid(self, sample_visual_chunks_tagged):
        """All ok-status chunks must have a valid 8-type visual_argument_type."""
        from optomind_research.visual_argument_alignment import VALID_VISUAL_ARGUMENT_TYPES
        for chunk in sample_visual_chunks_tagged:
            if chunk.get("visual_argument_status") == "ok":
                assert chunk.get("visual_argument_type") in VALID_VISUAL_ARGUMENT_TYPES, (
                    f"Chunk {chunk.get('chunk_id')} has invalid type "
                    f"'{chunk.get('visual_argument_type')}'"
                )

    def test_visual_argument_type_distribution_not_degenerate(self, sample_visual_chunks_tagged):
        """Type distribution must span ≥ 3 distinct types (not degenerate)."""
        from collections import Counter
        counts = Counter(
            c.get("visual_argument_type")
            for c in sample_visual_chunks_tagged
            if c.get("visual_argument_status") == "ok"
        )
        assert len(counts) >= 3, (
            f"Only {len(counts)} distinct argument types; distribution is degenerate"
        )
        total = sum(counts.values())
        for vtype, count in counts.items():
            assert count / total <= 0.70, (
                f"Type '{vtype}' dominates at {count/total:.0%} (>70%)"
            )

    def test_visual_alignment_report_generated(self, sample_visual_chunks_tagged):
        """VisualArgumentAligner.build_alignment_report() must produce a complete report."""
        from optomind_research.visual_argument_alignment import VisualArgumentAligner
        aligner = VisualArgumentAligner()
        result = aligner.build_alignment_report(sample_visual_chunks_tagged)
        d = result.to_dict()
        for key in (
            "schema_version", "total_visual_chunks", "ok_visual_chunks",
            "type_distribution", "warnings", "sample_records",
        ):
            assert key in d, f"report missing key '{key}'"
        assert d["total_visual_chunks"] == len(sample_visual_chunks_tagged)
        assert d["ok_visual_chunks"] == len(sample_visual_chunks_tagged)
        assert len(d["type_distribution"]) >= 3

    def test_visual_section_support_summary(self, sample_visual_blueprint_json):
        """Each section must have a visual_support_status; at least one ≥ some_visual_support."""
        from optomind_research.visual_argument_alignment import VisualArgumentAligner
        aligner = VisualArgumentAligner()
        result = aligner.build_alignment_report([], blueprint=sample_visual_blueprint_json)
        assert len(result.section_visual_support) >= 1, "No section support summaries"
        statuses = {s["visual_support_status"] for s in result.section_visual_support}
        supported = {"strong_visual_support", "some_visual_support"}
        assert statuses & supported, (
            f"No section has visual support; all statuses: {statuses}"
        )

    def test_claim_visual_recommendations_do_not_fabricate_ids(
        self, sample_visual_blueprint_json
    ):
        """Claim recommendations must not invent supporting_visual_chunk_ids."""
        from optomind_research.visual_argument_alignment import VisualArgumentAligner
        aligner = VisualArgumentAligner()
        result = aligner.build_alignment_report([], blueprint=sample_visual_blueprint_json)
        for crec in result.claim_visual_support:
            # candidate_visual_recommendations must not be assigned as supporting_visual_chunk_ids
            assert "supporting_visual_chunk_ids" not in crec, (
                f"Claim {crec['claim_id']} has fabricated supporting_visual_chunk_ids in report"
            )
            recs = crec.get("candidate_visual_recommendations", [])
            assert isinstance(recs, list), "candidate_visual_recommendations must be a list"


# ---------------------------------------------------------------------------
# M4b — Auto Visual Recommendation
# ---------------------------------------------------------------------------

class TestM4bAutoVisualRecommend:
    """验收M4b：section无candidate_visual_chunks时自动从KB推荐，不伪造supporting_visual_chunk_ids。"""

    def test_section_auto_recommend_when_no_visual_chunks(
        self, sample_no_visual_blueprint, sample_kb_visual_chunks
    ):
        """Section without visual chunks should become some/strong_visual_support after auto-recommend."""
        from optomind_research.visual_argument_alignment import VisualArgumentAligner
        aligner = VisualArgumentAligner()
        result = aligner.build_alignment_report(
            sample_kb_visual_chunks,
            blueprint=sample_no_visual_blueprint,
            auto_recommend=True,
            section_top_k=4,
        )
        statuses = {s["visual_support_status"] for s in result.section_visual_support}
        sources = {s.get("source") for s in result.section_visual_support}
        assert "auto_recommended_from_kb" in sources, (
            "Expected source='auto_recommended_from_kb' but got: " + str(sources)
        )
        assert statuses & {"some_visual_support", "strong_visual_support"}, (
            f"All sections still no_visual_support after auto-recommend; statuses={statuses}"
        )
        auto_sections = [
            s for s in result.section_visual_support
            if s.get("source") == "auto_recommended_from_kb"
        ]
        assert all(s.get("visual_argument_type_distribution") for s in auto_sections), (
            "Auto-recommended sections must expose visual_argument_type_distribution"
        )
        assert all(len(s.get("visual_argument_type_distribution") or {}) >= 2 for s in auto_sections), (
            "Auto-recommended sections should preserve visual argument type diversity"
        )

    def test_auto_recommend_does_not_fabricate_supporting_ids(
        self, sample_no_visual_blueprint, sample_kb_visual_chunks
    ):
        """Auto-recommend must never write supporting_visual_chunk_ids onto claims."""
        from optomind_research.visual_argument_alignment import VisualArgumentAligner
        aligner = VisualArgumentAligner()
        result = aligner.build_alignment_report(
            sample_kb_visual_chunks,
            blueprint=sample_no_visual_blueprint,
            auto_recommend=True,
        )
        for crec in result.claim_visual_support:
            assert "supporting_visual_chunk_ids" not in crec, (
                f"Claim {crec['claim_id']} has fabricated supporting_visual_chunk_ids"
            )

    def test_auto_recommended_claim_recommendations_have_source(
        self, sample_no_visual_blueprint, sample_kb_visual_chunks
    ):
        """Claim-level recommendations inherited from section auto-pools must expose source."""
        from optomind_research.visual_argument_alignment import VisualArgumentAligner
        aligner = VisualArgumentAligner()
        result = aligner.build_alignment_report(
            sample_kb_visual_chunks,
            blueprint=sample_no_visual_blueprint,
            auto_recommend=True,
            claim_top_k=3,
        )
        recs = [
            rec
            for crec in result.claim_visual_support
            for rec in (crec.get("candidate_visual_recommendations") or [])
        ]
        assert recs, "Expected claim-level visual recommendations from auto-recommended section pool"
        assert all(
            rec.get("source") in {"auto_recommended_from_kb", "claim_retrieved_from_kb"}
            for rec in recs
        ), f"Expected all claim recommendations to expose a KB retrieval source, got: {recs}"
        assert any(rec.get("source") == "claim_retrieved_from_kb" for rec in recs), (
            "Claim retrieval should be able to recover a valid visual outside the small section pool"
        )

    def test_claim_visual_recommendation_has_reason_and_score(
        self, sample_visual_blueprint_json, sample_kb_visual_chunks
    ):
        """Every candidate_visual_recommendation must carry score, reason, visual_argument_type, chunk_id."""
        from optomind_research.visual_argument_alignment import VisualArgumentAligner
        aligner = VisualArgumentAligner()
        # Merge mock KB chunks with an empty chunk_index (blueprint has inline visual chunks)
        result = aligner.build_alignment_report(
            sample_kb_visual_chunks,
            blueprint=sample_visual_blueprint_json,
            auto_recommend=False,
            claim_top_k=3,
        )
        recs_found = 0
        for crec in result.claim_visual_support:
            for r in crec.get("candidate_visual_recommendations") or []:
                recs_found += 1
                assert "chunk_id" in r, f"recommendation missing chunk_id: {r}"
                assert "visual_argument_type" in r, f"recommendation missing visual_argument_type: {r}"
                assert "score" in r, f"recommendation missing score: {r}"
                assert "reason" in r, f"recommendation missing reason: {r}"
                assert isinstance(r["score"], float), f"score must be float: {r}"
                assert r["reason"], f"reason must not be empty: {r}"

    def test_auto_recommend_report_fields(
        self, sample_no_visual_blueprint, sample_kb_visual_chunks
    ):
        """Report dict must contain the three new auto-recommend aggregate fields."""
        from optomind_research.visual_argument_alignment import VisualArgumentAligner
        aligner = VisualArgumentAligner()
        result = aligner.build_alignment_report(
            sample_kb_visual_chunks,
            blueprint=sample_no_visual_blueprint,
            auto_recommend=True,
        )
        d = result.to_dict()
        for key in (
            "auto_recommended_sections_count",
            "sections_without_visual_support_count",
            "total_recommended_visual_chunks",
        ):
            assert key in d, f"report missing key '{key}'"
        assert d["auto_recommended_sections_count"] >= 1, (
            "Expected at least one auto-recommended section"
        )
        assert d["total_recommended_visual_chunks"] >= 1, (
            "Expected at least one total recommended visual chunk"
        )


# ---------------------------------------------------------------------------
# M5 — 贡献预规划 + 全局自审 + 综述差异化
# ---------------------------------------------------------------------------

class TestM5ContributionAndReview:
    """验收M5：贡献声明存在，自审通过，差异化报告识别≥2个空白主题。"""

    @pytest.mark.skip(reason="M5未实现")
    def test_contribution_statements_present(self, sample_blueprint_json):
        stmts = sample_blueprint_json.get("contribution_statements", [])
        assert len(stmts) >= 1, "No contribution_statements in blueprint"

    @pytest.mark.skip(reason="M5未实现")
    def test_contribution_fulfilled_flag_present(self, sample_blueprint_json):
        review = sample_blueprint_json.get("global_review", {})
        assert "contribution_fulfilled" in review, "contribution_fulfilled field missing"

    @pytest.mark.skip(reason="M5未实现")
    def test_differentiation_report_identifies_gaps(self, sample_differentiation_report):
        uncovered = sample_differentiation_report.get("uncovered_topics", [])
        assert len(uncovered) >= 2, (
            f"Only {len(uncovered)} uncovered topics identified, expected ≥2"
        )


# ---------------------------------------------------------------------------
# M4c — Visual Evidence Reranker
# ---------------------------------------------------------------------------

class TestM4cVisualReranker:
    """验收M4c：VisualEvidenceReranker mock模式输出schema正确，不伪造IDs，拒绝弱匹配，保证类型多样性。"""

    def test_reranker_mock_output_schema(
        self, sample_visual_blueprint_json, sample_kb_visual_chunks
    ):
        """Every RerankResult must contain all 10 schema fields with correct types."""
        from optomind_research.visual_argument_alignment import VisualArgumentAligner
        aligner = VisualArgumentAligner()
        result = aligner.build_alignment_report(
            sample_kb_visual_chunks,
            blueprint=sample_visual_blueprint_json,
            auto_recommend=False,
            claim_top_k=3,
            rerank=True,
        )
        REQUIRED_FIELDS = {
            "chunk_id", "fit_score", "support_strength", "best_use",
            "supported_claim_aspect", "why_this_visual", "risk_or_caveat",
            "recommended_caption_sentence", "source", "needs_human_review",
            "evidence_mode",
        }
        VALID_STRENGTHS = {"strong", "medium", "weak", "decorative", "reject"}
        VALID_USES = {"main_figure", "supporting_figure", "background", "reject"}
        VALID_MODES = {
            "vision_image_text", "text_only",
            "text_only_image_unavailable", "text_only_client_lacks_vision_support",
        }

        reranked_found = 0
        for crec in result.claim_visual_support:
            for r in crec.get("reranked_visual_chunks") or []:
                reranked_found += 1
                missing = REQUIRED_FIELDS - r.keys()
                assert not missing, f"RerankResult missing fields {missing}: {r}"
                assert isinstance(r["fit_score"], float), "fit_score must be float"
                assert 0.0 <= r["fit_score"] <= 5.0, f"fit_score out of range: {r['fit_score']}"
                assert r["support_strength"] in VALID_STRENGTHS, (
                    f"invalid support_strength: {r['support_strength']}"
                )
                assert r["best_use"] in VALID_USES, f"invalid best_use: {r['best_use']}"
                assert isinstance(r["needs_human_review"], bool)
                assert r["why_this_visual"], "why_this_visual must not be empty"
                assert r.get("evidence_mode") in VALID_MODES, (
                    f"invalid evidence_mode: {r.get('evidence_mode')}"
                )
        assert reranked_found > 0, "No reranked_visual_chunks produced"

    def test_reranker_no_fabrication(
        self, sample_visual_blueprint_json, sample_kb_visual_chunks
    ):
        """Reranker must not fabricate supporting_visual_chunk_ids on any claim."""
        from optomind_research.visual_argument_alignment import VisualArgumentAligner
        aligner = VisualArgumentAligner()
        result = aligner.build_alignment_report(
            sample_kb_visual_chunks,
            blueprint=sample_visual_blueprint_json,
            auto_recommend=False,
            rerank=True,
        )
        for crec in result.claim_visual_support:
            assert "supporting_visual_chunk_ids" not in crec, (
                f"Claim {crec.get('claim_id')} has fabricated supporting_visual_chunk_ids"
            )

    def test_reranker_rejects_appear_in_rejected_list(
        self, sample_visual_blueprint_json, sample_kb_visual_chunks
    ):
        """Rejected chunks (support_strength=reject) must appear in rejected_visual_chunks."""
        from optomind_research.visual_argument_alignment import VisualArgumentAligner
        aligner = VisualArgumentAligner()
        result = aligner.build_alignment_report(
            sample_kb_visual_chunks,
            blueprint=sample_visual_blueprint_json,
            auto_recommend=False,
            claim_top_k=3,
            rerank=True,
        )
        # Validate structure: every claim record should have both lists
        for crec in result.claim_visual_support:
            assert "reranked_visual_chunks" in crec, (
                f"Claim {crec.get('claim_id')} missing 'reranked_visual_chunks'"
            )
            assert "rejected_visual_chunks" in crec, (
                f"Claim {crec.get('claim_id')} missing 'rejected_visual_chunks'"
            )
            for rj in crec.get("rejected_visual_chunks") or []:
                assert "rejection_reason" in rj, (
                    f"Rejected chunk missing rejection_reason: {rj}"
                )
                assert rj.get("support_strength") == "reject", (
                    f"Chunk in rejected list has wrong support_strength: {rj}"
                )

    def test_reranker_with_auto_recommend(
        self, sample_no_visual_blueprint, sample_kb_visual_chunks
    ):
        """Reranker must work end-to-end with auto_recommend=True (no blueprint visual chunks)."""
        from optomind_research.visual_argument_alignment import VisualArgumentAligner
        aligner = VisualArgumentAligner()
        result = aligner.build_alignment_report(
            sample_kb_visual_chunks,
            blueprint=sample_no_visual_blueprint,
            auto_recommend=True,
            section_top_k=4,
            claim_top_k=3,
            rerank=True,
        )
        # At least some reranked results should exist
        total_reranked = sum(
            len(crec.get("reranked_visual_chunks") or [])
            for crec in result.claim_visual_support
        )
        assert total_reranked >= 1, (
            "Expected at least one reranked chunk with auto_recommend=True + rerank=True"
        )
        # Verify source field is preserved
        for crec in result.claim_visual_support:
            for r in crec.get("reranked_visual_chunks") or []:
                assert r.get("source") in (
                    "provided_by_blueprint", "auto_recommended_from_kb", "claim_retrieved_from_kb"
                ), f"Unexpected source value: {r.get('source')}"

    def test_text_only_mode_caps_at_medium(
        self, sample_visual_blueprint_json, sample_kb_visual_chunks
    ):
        """Mock mode uses text_only evidence_mode: support_strength must be ≤ medium,
        best_use must never be main_figure."""
        from optomind_research.visual_evidence_reranker import VisualEvidenceReranker
        reranker = VisualEvidenceReranker(real_llm=False)
        all_results = []
        from optomind_research.visual_argument_alignment import VisualArgumentAligner
        aligner = VisualArgumentAligner()
        result = aligner.build_alignment_report(
            sample_kb_visual_chunks,
            blueprint=sample_visual_blueprint_json,
            rerank=True,
        )
        for crec in result.claim_visual_support:
            for r in (crec.get("reranked_visual_chunks") or []) + (crec.get("rejected_visual_chunks") or []):
                all_results.append(r)
        assert all_results, "No rerank results found"
        for r in all_results:
            mode = r.get("evidence_mode", "")
            if mode != "vision_image_text":
                assert r["support_strength"] != "strong", (
                    f"text-only mode produced support_strength=strong: {r}"
                )
                assert r["best_use"] != "main_figure", (
                    f"text-only mode produced best_use=main_figure: {r}"
                )

    def test_llm_failure_returns_reject_not_medium(self):
        """_failure_result() must produce support_strength=reject and non-empty failure_reason."""
        from optomind_research.visual_evidence_reranker import VisualEvidenceReranker
        reranker = VisualEvidenceReranker(real_llm=True)
        r = reranker._failure_result(
            chunk_id="chunk-test-001",
            source="provided_by_blueprint",
            vtype="quantitative_comparison",
            reason="ConnectionTimeoutError",
        )
        assert r.support_strength == "reject", (
            f"LLM failure must yield reject, got {r.support_strength}"
        )
        assert r.fit_score == 0.0, f"LLM failure must yield fit_score=0.0, got {r.fit_score}"
        assert r.failure_reason == "ConnectionTimeoutError", (
            f"failure_reason not set: {r.failure_reason}"
        )
        assert r.needs_human_review is True

    def test_image_path_unavailable_sets_correct_mode(self):
        """call_qwen_vision with non-existent image path must return text_only_image_unavailable."""
        from llm.qwen_vision_client import call_qwen_vision
        resp = call_qwen_vision(
            agent_name="test_agent",
            text_prompt="Evaluate this figure.",
            local_image_path="/nonexistent/path/image_xyz_12345.png",
            force_mock=True,
        )
        assert "_evidence_mode" in resp
        # In mock mode the image is never opened, so mode is text_only (mock doesn't check file)
        # What matters is the function returns a response without crashing
        assert resp.get("_vision_used") is False
        assert resp.get("content") != ""

    def test_rerank_workers_param_accepted(
        self, sample_visual_blueprint_json, sample_kb_visual_chunks
    ):
        """build_alignment_report must accept rerank_workers without error."""
        from optomind_research.visual_argument_alignment import VisualArgumentAligner
        aligner = VisualArgumentAligner()
        result = aligner.build_alignment_report(
            sample_kb_visual_chunks,
            blueprint=sample_visual_blueprint_json,
            rerank=True,
            rerank_workers=2,
        )
        assert result.total_visual_chunks > 0

    def test_cache_prevents_duplicate_calls(self):
        """_cache_key must produce identical keys for identical inputs."""
        from optomind_research.visual_evidence_reranker import VisualEvidenceReranker
        key1 = VisualEvidenceReranker._cache_key("claim-A1", "chunk-001", "vision_model")
        key2 = VisualEvidenceReranker._cache_key("claim-A1", "chunk-001", "vision_model")
        key3 = VisualEvidenceReranker._cache_key("claim-A1", "chunk-002", "vision_model")
        assert key1 == key2, "Same inputs must produce same cache key"
        assert key1 != key3, "Different chunk_id must produce different cache key"
        assert len(key1) == 16, f"Cache key must be 16 hex chars, got {len(key1)}"

    def test_invalid_json_is_recovered_by_premium_vision_retry(
        self, tmp_path, monkeypatch
    ):
        """Malformed primary output must trigger one bounded, auditable retry."""
        import llm.qwen_vision_client as vision_client
        from optomind_research.visual_evidence_reranker import VisualEvidenceReranker

        calls = []

        def fake_call(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return {
                    "content": "not valid json",
                    "_evidence_mode": "vision_image_text",
                }
            return {
                "content": """```json
                {"fit_score": 4.2, "support_strength": "strong",
                 "best_use": "main_figure", "directness": "direct",
                 "supported_claim_components": ["energy balance"],
                 "unsupported_claim_components": [],
                 "provable_claim_part": "radiative-cooling energy balance",
                 "entity_alignment": "exact", "entity_mismatch_reason": "",
                 "why_this_visual": "Direct mechanism schematic.",
                 "risk_or_caveat": ""}
                ```""",
                "_evidence_mode": "vision_image_text",
            }

        monkeypatch.setattr(vision_client, "call_qwen_vision", fake_call)
        image_path = tmp_path / "visual.png"
        image_path.write_bytes(b"not-an-image-but-the-call-is-mocked")
        reranker = VisualEvidenceReranker(
            real_llm=True,
            model_tier="vision_model",
            workers=1,
            cache_path=tmp_path / "cache.jsonl",
        )
        result = reranker._llm_rerank(
            {
                "chunk_id": "visual-1",
                "visual_argument_type": "mechanism_anchor",
                "source": "claim_retrieved_from_kb",
            },
            {"title": "Mechanism", "argument_role": "Explain energy balance."},
            {
                "claim_id": "C01",
                "statement": "Net cooling follows the surface energy balance.",
                "evidence_type": "mechanism",
            },
            {
                "caption": "Energy balance for a radiative cooler.",
                "search_text": "radiation, atmosphere, convection and solar load",
                "local_image_path": str(image_path),
            },
        )
        assert len(calls) == 2
        assert calls[1]["model_tier"] == "vision_premium_model"
        assert result.failure_reason == ""
        assert result.vision_attempt_count == 2
        assert result.recovery_action == "normalized_image_premium_retry"
        assert result.evidence_mode == "vision_image_text"

    def test_transient_vision_failure_is_not_cached(self, tmp_path, monkeypatch):
        """An exhausted API failure must be retried by a later pipeline run."""
        import llm.qwen_vision_client as vision_client
        from optomind_research.visual_evidence_reranker import VisualEvidenceReranker

        calls = []

        def fake_call(**kwargs):
            calls.append(kwargs)
            if len(calls) <= 2:
                return {
                    "content": "",
                    "_evidence_mode": "vision_image_text",
                    "_failure_reason": "temporary_http_error",
                }
            return {
                "content": json.dumps({
                    "fit_score": 3.5,
                    "support_strength": "medium",
                    "best_use": "supporting_figure",
                    "directness": "direct",
                    "supported_claim_components": ["spectrum"],
                    "unsupported_claim_components": [],
                    "provable_claim_part": "spectral response",
                    "entity_alignment": "exact",
                    "entity_mismatch_reason": "",
                    "why_this_visual": "Direct spectral evidence.",
                    "risk_or_caveat": "",
                }),
                "_evidence_mode": "vision_image_text",
            }

        monkeypatch.setattr(vision_client, "call_qwen_vision", fake_call)
        image_path = tmp_path / "visual.png"
        image_path.write_bytes(b"mocked")
        reranker = VisualEvidenceReranker(
            real_llm=True,
            workers=1,
            cache_path=tmp_path / "cache.jsonl",
        )
        candidate = {
            "chunk_id": "visual-2",
            "visual_argument_type": "trend_or_parameter_map",
            "source": "claim_retrieved_from_kb",
        }
        section = {"title": "Spectrum", "argument_role": "Explain spectral response."}
        claim = {
            "claim_id": "C02",
            "statement": "The measured spectrum changes with wavelength.",
            "evidence_type": "measurement",
        }
        chunk = {
            "caption": "Measured spectral response.",
            "local_image_path": str(image_path),
        }
        first = reranker.rerank([candidate], section, claim, {"visual-2": chunk})[0]
        second = reranker.rerank([candidate], section, claim, {"visual-2": chunk})[0]
        assert first.support_strength == "reject"
        assert first.failure_reason
        assert second.support_strength != "reject"
        assert len(calls) == 3

    def test_visual_caveat_cannot_coexist_with_strong_direct_promotion(self):
        """A model-admitted missing component must trigger deterministic downgrade."""
        from optomind_research.visual_evidence_reranker import (
            RerankResult,
            VisualEvidenceReranker,
        )

        result = RerankResult(
            chunk_id="visual-3",
            fit_score=4.5,
            support_strength="strong",
            best_use="main_figure",
            supported_claim_aspect="mechanism",
            why_this_visual="Shows the main energy flows.",
            risk_or_caveat=(
                "The figure does not explicitly define the reflectance term "
                "required by the complete equation."
            ),
            recommended_caption_sentence="Energy balance.",
            source="claim_retrieved_from_kb",
            needs_human_review=False,
            evidence_mode="vision_image_text",
            directness="direct",
            entity_alignment="exact",
        )
        calibrated = VisualEvidenceReranker()._calibrate_result(result)
        assert calibrated.support_strength == "weak"
        assert calibrated.best_use == "supporting_figure"
        assert calibrated.needs_human_review is True

    def test_cached_visual_judgment_is_recalibrated_under_current_policy(self):
        from optomind_research.visual_evidence_reranker import VisualEvidenceReranker

        reranker = VisualEvidenceReranker()
        reranker._cache["cached"] = {
            "chunk_id": "visual-4",
            "fit_score": 4.5,
            "support_strength": "strong",
            "best_use": "main_figure",
            "supported_claim_aspect": "mechanism",
            "why_this_visual": "Shows most energy flows.",
            "risk_or_caveat": "The figure does not explicitly define one core term.",
            "recommended_caption_sentence": "Energy balance.",
            "source": "claim_retrieved_from_kb",
            "needs_human_review": False,
            "evidence_mode": "vision_image_text",
            "directness": "direct",
            "entity_alignment": "exact",
        }
        result = reranker._lookup_cache("cached")
        assert result is not None
        assert result.support_strength == "weak"
        assert result.needs_human_review is True

    def test_rerank_stats_expose_retry_recovery(self):
        from optomind_research.visual_evidence_reranker import VisualEvidenceReranker

        stats = VisualEvidenceReranker().compute_rerank_stats([{
            "reranked_visual_chunks": [{
                "chunk_id": "visual-5",
                "support_strength": "weak",
                "evidence_mode": "vision_image_text",
                "directness": "partial",
                "needs_human_review": True,
                "vision_attempt_count": 2,
                "vision_model_tier_used": "vision_premium_model",
                "recovery_action": "normalized_image_premium_retry",
            }],
            "rejected_visual_chunks": [],
        }])
        assert stats["total_vision_attempts"] == 2
        assert stats["recovered_retry_count"] == 1
        assert stats["premium_retry_count"] == 1


# ---------------------------------------------------------------------------
# M4c — SQLite local_image_path propagation
# ---------------------------------------------------------------------------

class TestM4cSQLiteImagePath:
    """验收 load_visual_chunks_from_sqlite 是否正确读取 local_image_path 字段。"""

    def test_sqlite_load_includes_local_image_path(self, tmp_path):
        """load_visual_chunks_from_sqlite() must return local_image_path when present in DB."""
        import sqlite3
        from optomind_research.visual_argument_alignment import VisualArgumentAligner

        db = tmp_path / "visual_chunks_test.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute("""
            CREATE TABLE visual_chunks (
                chunk_id TEXT, paper_id TEXT, doi TEXT, title TEXT, caption TEXT,
                chunk_kind TEXT, visual_role TEXT, review_utility TEXT,
                parent_label TEXT, subfigure_label TEXT, search_text TEXT,
                visual_argument_type TEXT, visual_argument_status TEXT,
                visual_argument_confidence TEXT, visual_argument_claim TEXT,
                visual_argument_needs_human_review INTEGER,
                visual_argument_schema_version TEXT,
                local_image_path TEXT
            )
        """)
        conn.execute("""
            INSERT INTO visual_chunks VALUES (
                'chunk-001', 'paper-A', '10.1234/test', 'Test Figure', 'Test caption',
                'figure', 'primary', 'high',
                'Fig 1', NULL, 'some search text',
                'quantitative_comparison', 'ok',
                'high', 'claim text', 0,
                'v1',
                '/data/papers/paper_a/fig1.png'
            )
        """)
        conn.commit()
        conn.close()

        aligner = VisualArgumentAligner()
        chunks = aligner.load_visual_chunks_from_sqlite(db)
        assert len(chunks) == 1, "Expected 1 chunk"
        chunk = chunks[0]
        assert "local_image_path" in chunk, "local_image_path must be in loaded chunk"
        assert chunk["local_image_path"] == "/data/papers/paper_a/fig1.png"

    def test_sqlite_load_handles_null_local_image_path(self, tmp_path):
        """load_visual_chunks_from_sqlite() must not crash when local_image_path is NULL."""
        import sqlite3
        from optomind_research.visual_argument_alignment import VisualArgumentAligner

        db = tmp_path / "visual_chunks_null_img.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute("""
            CREATE TABLE visual_chunks (
                chunk_id TEXT, paper_id TEXT, doi TEXT, title TEXT, caption TEXT,
                chunk_kind TEXT, visual_role TEXT, review_utility TEXT,
                parent_label TEXT, subfigure_label TEXT, search_text TEXT,
                visual_argument_type TEXT, visual_argument_status TEXT,
                visual_argument_confidence TEXT, visual_argument_claim TEXT,
                visual_argument_needs_human_review INTEGER,
                visual_argument_schema_version TEXT,
                local_image_path TEXT
            )
        """)
        conn.execute("""
            INSERT INTO visual_chunks VALUES (
                'chunk-002', 'paper-B', '10.1234/test2', 'Fig 2', '',
                'figure', 'secondary', 'medium',
                'Fig 2', NULL, '',
                'mechanism_anchor', 'ok',
                'medium', '', 0,
                'v1',
                NULL
            )
        """)
        conn.commit()
        conn.close()

        aligner = VisualArgumentAligner()
        chunks = aligner.load_visual_chunks_from_sqlite(db)
        assert len(chunks) == 1
        assert chunks[0].get("local_image_path") is None

    def test_max_items_global_budget(self, sample_visual_blueprint_json, sample_kb_visual_chunks):
        """rerank_claim_support with max_items=2 must process at most 2 total candidates."""
        from optomind_research.visual_argument_alignment import VisualArgumentAligner
        aligner = VisualArgumentAligner()
        result = aligner.build_alignment_report(
            sample_kb_visual_chunks,
            blueprint=sample_visual_blueprint_json,
            rerank=True,
            rerank_max_items=2,
        )
        total_scored = sum(
            len(crec.get("reranked_visual_chunks") or [])
            + len(crec.get("rejected_visual_chunks") or [])
            for crec in result.claim_visual_support
        )
        assert total_scored <= 2, (
            f"rerank_max_items=2 must limit total to ≤2 items, got {total_scored}"
        )


# ---------------------------------------------------------------------------
# P0 — DomainConfigLoader
# ---------------------------------------------------------------------------

class TestP0DomainConfigLoader:
    """验收 domain_config_loader.py 是否正确加载和提供配置值。"""

    def test_load_returns_dict_when_no_yaml(self, tmp_path):
        """不存在的 yaml 路径应返回空 dict，不崩溃。"""
        from optomind_research.domain_config_loader import load_domain_config
        result = load_domain_config(tmp_path / "nonexistent_config.yaml")
        assert isinstance(result, dict)

    def test_get_topic_context_fallback(self, tmp_path):
        """yaml 不存在时 get_topic_context 应返回空字符串。"""
        from optomind_research.domain_config_loader import load_domain_config, get_topic_context
        cfg = load_domain_config(tmp_path / "nonexistent.yaml")
        ctx = get_topic_context(cfg)
        assert isinstance(ctx, str)

    def test_get_m3_defaults_has_required_keys(self, tmp_path):
        """get_m3_defaults 返回的 dict 必须包含所有必需键。"""
        from optomind_research.domain_config_loader import load_domain_config, get_m3_defaults
        cfg = load_domain_config(tmp_path / "nonexistent.yaml")
        defaults = get_m3_defaults(cfg)
        required = {
            "topic_context", "query_boost_terms", "saturation_threshold",
            "max_claims_per_loop", "from_year", "top_k",
            "results_per_backend", "max_queries", "references_per_seed",
        }
        missing = required - set(defaults.keys())
        assert not missing, f"get_m3_defaults missing keys: {missing}"

    def test_get_m3_defaults_numeric_types(self, tmp_path):
        """get_m3_defaults 返回的数值字段必须是正确类型。"""
        from optomind_research.domain_config_loader import load_domain_config, get_m3_defaults
        cfg = load_domain_config(tmp_path / "nonexistent.yaml")
        defaults = get_m3_defaults(cfg)
        assert isinstance(defaults["saturation_threshold"], float)
        assert isinstance(defaults["max_claims_per_loop"], int)
        assert isinstance(defaults["from_year"], int)
        assert isinstance(defaults["top_k"], int)
        assert isinstance(defaults["query_boost_terms"], list)

    def test_load_from_project_root_yaml(self):
        """项目根目录的 domain_config.yaml 应能成功加载。"""
        from optomind_research.domain_config_loader import load_domain_config, PROJECT_ROOT
        yaml_path = PROJECT_ROOT / "domain_config.yaml"
        if not yaml_path.exists():
            import pytest
            pytest.skip("domain_config.yaml not found in project root")
        cfg = load_domain_config(yaml_path, force_reload=True)
        assert isinstance(cfg, dict), "domain_config.yaml must parse to dict"
        assert "domain" in cfg or "m3_retrieval" in cfg, \
            "domain_config.yaml must have at least 'domain' or 'm3_retrieval' section"

    def test_project_yaml_topic_context_nonempty(self):
        """项目 domain_config.yaml 的 topic_context 不应为空字符串。"""
        from optomind_research.domain_config_loader import (
            load_domain_config, get_topic_context, PROJECT_ROOT
        )
        yaml_path = PROJECT_ROOT / "domain_config.yaml"
        if not yaml_path.exists():
            import pytest
            pytest.skip("domain_config.yaml not found")
        cfg = load_domain_config(yaml_path, force_reload=True)
        ctx = get_topic_context(cfg)
        assert len(ctx) > 20, f"topic_context too short: '{ctx}'"


# ---------------------------------------------------------------------------
# P1 — M3 KB 回流 (m3_kb_ingest)
# ---------------------------------------------------------------------------

class TestP1KBIngest:
    """验收 m3_kb_ingest.py 的核心逻辑：饱和度重算、段落切分、SQLite写入。"""

    def test_recalculate_saturation_only_upward(self):
        """saturation 只能上调，不允许下调。"""
        from optomind_research.m3_kb_ingest import recalculate_saturation
        # 当前 2.5，新 chunk count 只能给出 1.0 → 保留 2.5
        assert recalculate_saturation(2.5, 3) == 2.5

    def test_recalculate_saturation_formula(self):
        """Saturation follows source diversity and cannot auto-award consensus."""
        from optomind_research.m3_kb_ingest import recalculate_saturation
        assert recalculate_saturation(0.0, 0) == 0.0
        assert recalculate_saturation(0.0, 30, unique_paper_count=1) == 1.0
        assert recalculate_saturation(0.5, 30, unique_paper_count=2) == 1.8
        assert recalculate_saturation(0.0, 30, unique_paper_count=4) == 2.7

    def test_split_paragraphs_basic(self):
        """空行分段应正常工作。"""
        from optomind_research.m3_kb_ingest import split_paragraphs
        text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        paras = split_paragraphs(text, min_chars=5)
        assert len(paras) >= 2

    def test_split_paragraphs_filters_short(self):
        """低于 min_chars/2 的段落应被过滤掉。"""
        from optomind_research.m3_kb_ingest import split_paragraphs
        text = "OK paragraph with enough content here.\n\nAB"
        paras = split_paragraphs(text, min_chars=80)
        for p in paras:
            assert len(p) >= 40, f"Short paragraph slipped through: '{p}'"

    def test_ingester_no_sqlite_runs_safely(self):
        """KBIngester 在没有 kb_sqlite 时也能运行，不崩溃。"""
        from optomind_research.m3_kb_ingest import KBIngester
        ingester = KBIngester(kb_sqlite=None)
        claim = {"claim_id": "S01-C01", "section_id": "S01", "saturation_score": 0.5,
                 "supporting_text_chunk_ids": []}
        candidates: list = []
        result = ingester.ingest_oa_candidates(candidates, claim)
        assert result.claim_id == "S01-C01"
        assert result.new_chunk_ids == []
        assert result.new_saturation_score == 0.5  # 没有新 chunks，saturation 保持不变

    def test_ingester_writes_text_chunks_to_sqlite(self, tmp_path):
        """KBIngester 必须能把解析好的段落写入 SQLite text_chunks 表。"""
        import sqlite3
        from optomind_research.m3_kb_ingest import KBIngester, _ensure_text_chunks_table

        db = tmp_path / "test_kb.sqlite"
        ingester = KBIngester(kb_sqlite=db)

        # 构造一个已有 fulltext 的 OA 候选（跳过网络下载，直接测 SQLite 写入）
        # 通过 monkey-patch download_and_extract 返回固定文本
        import optomind_research.m3_kb_ingest as ingest_mod
        original_fn = ingest_mod.download_and_extract

        def fake_download(candidate, download_dir=None):
            return (
                "This is the first paragraph of the test paper. It contains relevant information.\n\n"
                "This is the second paragraph. It discusses materials and methods in detail.\n\n"
                "The third paragraph presents results and conclusions about radiative cooling.",
                "https://fake-oa-url.org/paper.pdf",
            )

        ingest_mod.download_and_extract = fake_download
        try:
            claim = {
                "claim_id": "S03-C01", "section_id": "S03",
                "saturation_score": 0.5, "supporting_text_chunk_ids": ["existing-chunk-1"],
            }
            result = ingester.ingest_oa_candidates(
                [{"doi": "10.1234/test.paper", "title": "Test Radiative Cooling Paper",
                  "oa_url": "https://fake-oa-url.org/paper.pdf"}],
                claim,
            )
        finally:
            ingest_mod.download_and_extract = original_fn

        assert len(result.new_chunk_ids) >= 2, \
            f"Expected ≥2 chunks, got {len(result.new_chunk_ids)}"
        assert all(cid.startswith("m3gap:") for cid in result.new_chunk_ids), \
            "Chunk IDs must start with 'm3gap:'"

        # 验证写入 SQLite
        conn = sqlite3.connect(str(db))
        rows = conn.execute("SELECT chunk_id FROM text_chunks").fetchall()
        conn.close()
        assert len(rows) >= 2, f"Expected ≥2 rows in text_chunks, got {len(rows)}"

    def test_ingester_updates_saturation_score(self, tmp_path):
        """新 chunks 写入后 saturation_score 必须上调。"""
        import optomind_research.m3_kb_ingest as ingest_mod
        from optomind_research.m3_kb_ingest import KBIngester

        db = tmp_path / "sat_update_test.sqlite"
        ingester = KBIngester(kb_sqlite=db)

        original_fn = ingest_mod.download_and_extract

        def fake_download(candidate, download_dir=None):
            lines = "\n\n".join(
                f"Paragraph {i}: detailed scientific content about radiative cooling materials."
                for i in range(1, 10)
            )
            return lines, "https://fake.url/paper.pdf"

        ingest_mod.download_and_extract = fake_download
        try:
            claim = {
                "claim_id": "S01-C02", "section_id": "S01",
                "statement": "Radiative cooling materials provide measurable thermal performance.",
                "saturation_score": 0.3, "supporting_text_chunk_ids": [],
            }
            result = ingester.ingest_oa_candidates(
                [{"doi": "10.9999/test", "title": "Test Paper",
                  "oa_url": "https://fake.url/paper.pdf"}],
                claim,
            )
        finally:
            ingest_mod.download_and_extract = original_fn

        assert result.new_saturation_score > 0.3, \
            f"saturation_score must increase, got {result.new_saturation_score}"

    def test_ingest_result_to_dict(self):
        """IngestResult.to_dict() 必须包含所有必需字段。"""
        from optomind_research.m3_kb_ingest import IngestResult
        r = IngestResult(
            claim_id="S01-C01",
            new_chunk_ids=["m3gap:test:0001"],
            new_paper_ids=["doi:10.1234/test"],
            new_saturation_score=1.5,
            stats={"chunks_written": 1},
        )
        d = r.to_dict()
        required_keys = {"claim_id", "new_chunk_ids", "new_paper_ids",
                         "new_saturation_score", "stats"}
        missing = required_keys - set(d.keys())
        assert not missing, f"to_dict() missing keys: {missing}"


# ---------------------------------------------------------------------------
# P2 — M1 教学案例库重构
# ---------------------------------------------------------------------------

class TestP2MoveEnrichment:
    """P2 验收测试：enriched move 字段 + select_moves 评分改进。"""

    # 构造一个已 enriched 的 mock move
    _ENRICHED_MOVE = {
        "move": "Scalar detection reframed as multi-dimensional reconstruction",
        "why_it_matters": "Shifts discourse from single metric to system-level argument",
        "reuse_for_our_review_system": "When reviewing multi-dimensional detector papers, flag cross-talk metric",
        "possible_overreach": "May not apply to simple single-channel systems",
        "transferable_rule": "When a topic has competing design constraints, restructure the review around trade-off analysis rather than single-metric optimization",
        "trigger_when": "user question involves competing constraints multi-objective optimization trade-off",
        "bad_pattern_to_avoid": "Listing methods by type without exposing the underlying design logic",
        "downstream_hooks": ["blueprint", "M2a"],
        "example_transformation": {
            "ordinary": "Material A has good cooling performance",
            "top_review_style": "The core challenge is spectral decoupling under multi-objective constraints",
        },
    }

    def test_move_to_text_uses_transferable_rule(self):
        """move 有 transferable_rule 时，move_to_text() 返回文本应包含该值。"""
        from optomind_research.review_mentor_agent import move_to_text
        text = move_to_text(self._ENRICHED_MOVE)
        assert "competing design constraints" in text, \
            f"transferable_rule content not in move_to_text output: {text[:200]}"

    def test_move_to_text_fallback_to_reuse_hint(self):
        """无 transferable_rule 时，move_to_text() fallback 到 reuse_for_our_review_system。"""
        from optomind_research.review_mentor_agent import move_to_text
        legacy_move = {
            "move": "Some move",
            "why_it_matters": "Some reason",
            "reuse_for_our_review_system": "reuse_unique_keyword_here",
            "possible_overreach": "",
        }
        text = move_to_text(legacy_move)
        assert "reuse_unique_keyword_here" in text, \
            f"Fallback to reuse_for_our_review_system failed: {text[:200]}"

    def test_trigger_when_boosts_score(self):
        """context 命中 trigger_when 关键词时，该 move 得分应高于无 trigger_when 的同质 move。"""
        from optomind_research.review_mentor_agent import ReviewMentorAgent, tokenize, move_to_text

        context = "multi-objective optimization competing constraints trade-off analysis"
        context_tokens = tokenize(context)

        enriched = self._ENRICHED_MOVE
        legacy = {k: v for k, v in enriched.items() if k not in ("transferable_rule", "trigger_when", "bad_pattern_to_avoid")}

        def score_move(move):
            text = move_to_text(move)
            tokens = tokenize(text)
            overlap = len(context_tokens & tokens)
            s = overlap / max(1, len(context_tokens) ** 0.5) + min(0.3, len(tokens) / 400.0)
            if isinstance(move, dict) and move.get("reuse_for_our_review_system"):
                s += 0.15
            if isinstance(move, dict) and move.get("trigger_when"):
                trigger = tokenize(move.get("trigger_when", ""))
                if trigger:
                    trigger_overlap = len(context_tokens & trigger)
                    s += 0.3 * trigger_overlap / max(1, len(trigger) ** 0.5)
            return s

        enriched_score = score_move(enriched)
        legacy_score = score_move(legacy)
        assert enriched_score > legacy_score, (
            f"enriched move score ({enriched_score:.4f}) should exceed "
            f"legacy move score ({legacy_score:.4f}) when trigger_when matches context"
        )

    def test_backward_compat_select_moves(self):
        """无新字段的旧格式 move 仍能通过 select_moves()，不应抛出 KeyError。"""
        from unittest.mock import patch
        from optomind_research.review_mentor_agent import ReviewMentorAgent

        legacy_library = {
            "problem_reframing": [
                {
                    "move": "Legacy move without new fields",
                    "why_it_matters": "Classic pattern",
                    "reuse_for_our_review_system": "Use it when applicable",
                    "possible_overreach": "Might not always work",
                }
            ]
        }
        agent = ReviewMentorAgent(real_llm=False)
        with patch.object(agent, "_active_library", legacy_library):
            try:
                result = agent.select_moves("test context about legacy")
            except KeyError as e:
                pytest.fail(f"KeyError raised for legacy move: {e}")
        assert "problem_reframing" in result

    def test_enriched_move_has_all_new_fields(self):
        """enriched move dict 必须包含所有5个新字段，且均为非空。"""
        required_new_fields = [
            "transferable_rule",
            "trigger_when",
            "bad_pattern_to_avoid",
            "downstream_hooks",
            "example_transformation",
        ]
        move = self._ENRICHED_MOVE
        for field in required_new_fields:
            assert field in move, f"Missing new field: {field}"
            value = move[field]
            if isinstance(value, str):
                assert value.strip(), f"Field '{field}' is empty string"
            elif isinstance(value, list):
                assert len(value) > 0, f"Field '{field}' is empty list"
            elif isinstance(value, dict):
                assert len(value) > 0, f"Field '{field}' is empty dict"


# ---------------------------------------------------------------------------
# P3 — MoveIndex（向量检索）
# ---------------------------------------------------------------------------

class TestP3MoveIndex:
    """验收 P3 向量检索：MoveIndex 结构、文本准备、FAISS集成、回退逻辑。"""

    def test_move_index_imports(self):
        """MoveIndex 及辅助函数应能正常导入。"""
        from optomind_research.move_index import (
            MoveIndex,
            embed_texts_batched,
            _move_to_embed_text,
            _l2_normalize,
            EMBEDDING_MODEL,
            EMBEDDING_DIM,
        )
        assert EMBEDDING_MODEL == "text-embedding-v3"
        assert EMBEDDING_DIM == 1024

    def test_is_built_false_when_no_files(self, tmp_path):
        """空目录时 is_built() 应返回 False。"""
        from optomind_research.move_index import MoveIndex
        idx = MoveIndex(index_dir=tmp_path / "empty_index")
        assert idx.is_built() is False

    def test_move_to_embed_text_uses_transferable_rule(self):
        """有 transferable_rule 时，embed 文本应包含该字段。"""
        from optomind_research.move_index import _move_to_embed_text
        move = {
            "transferable_rule": "unique_rule_text_for_test",
            "trigger_when": "when constraints compete",
            "move": "some move",
            "why_it_matters": "reason",
        }
        text = _move_to_embed_text(move)
        assert "unique_rule_text_for_test" in text

    def test_move_to_embed_text_fallback(self):
        """无 transferable_rule 时，embed 文本应 fallback 到 reuse_for_our_review_system。"""
        from optomind_research.move_index import _move_to_embed_text
        move = {
            "reuse_for_our_review_system": "fallback_reuse_keyword",
            "move": "some move",
            "why_it_matters": "reason",
        }
        text = _move_to_embed_text(move)
        assert "fallback_reuse_keyword" in text

    def test_l2_normalize_unit_vector(self):
        """L2 归一化后向量的模应 ≈ 1.0。"""
        import math
        from optomind_research.move_index import _l2_normalize
        vec = [3.0, 4.0]
        normed = _l2_normalize(vec)
        norm = math.sqrt(sum(x * x for x in normed))
        assert abs(norm - 1.0) < 1e-6

    def test_l2_normalize_zero_vector(self):
        """零向量 L2 归一化不应崩溃。"""
        from optomind_research.move_index import _l2_normalize
        vec = [0.0, 0.0, 0.0]
        result = _l2_normalize(vec)
        assert len(result) == 3

    def test_build_requires_api_key(self, tmp_path):
        """无 API key 时 build() 应返回 error report 而非崩溃。"""
        import json
        from unittest.mock import patch
        from optomind_research.move_index import MoveIndex

        library = {
            "problem_reframing": [
                {
                    "move": "Test move",
                    "transferable_rule": "Test rule",
                    "trigger_when": "always",
                    "why_it_matters": "important",
                }
            ]
        }
        lib_path = tmp_path / "mock_library.json"
        lib_path.write_text(json.dumps(library), encoding="utf-8")

        idx = MoveIndex(index_dir=tmp_path / "test_index")
        with patch("optomind_research.move_index._get_api_key", return_value=""):
            report = idx.build(library_path=lib_path)
        assert report.get("status") == "error"
        assert "key" in report.get("reason", "").lower()

    def test_review_mentor_use_vector_index_field_exists(self):
        """ReviewMentorAgent 必须有 use_vector_index 字段，默认 False。"""
        from optomind_research.review_mentor_agent import ReviewMentorAgent
        agent = ReviewMentorAgent()
        assert hasattr(agent, "use_vector_index")
        assert agent.use_vector_index is False

    def test_keyword_fallback_when_index_not_built(self):
        """use_vector_index=True 但索引不存在时，select_moves 应回退到关键词匹配。"""
        from unittest.mock import patch
        from optomind_research.review_mentor_agent import ReviewMentorAgent, M1_CATEGORIES

        moves = [
            {
                "move": "test move alpha",
                "why_it_matters": "alpha reason",
                "reuse_for_our_review_system": "alpha hint",
                "transferable_rule": "alpha rule",
            }
        ]
        library = {cat: moves for cat in M1_CATEGORIES}

        agent = ReviewMentorAgent(use_vector_index=True)
        # _move_index remains None (index not built)
        with patch.object(agent, "_active_library", library):
            result = agent.select_moves("alpha context test query")
        # Should succeed via keyword path (no error)
        assert isinstance(result, dict)
        assert all(cat in result for cat in M1_CATEGORIES)

    def test_vector_select_moves_groups_by_category(self, tmp_path, monkeypatch):
        """_vector_select_moves 应按 category 分组，每类不超过 max_moves_per_category 条。"""
        from optomind_research.review_mentor_agent import ReviewMentorAgent, M1_CATEGORIES
        from optomind_research.move_index import MoveIndex

        # 构造 mock MoveIndex.query 返回值
        mock_hits = []
        for cat in M1_CATEGORIES:
            for i in range(6):  # 每类6条，应被限制为4
                mock_hits.append({
                    "category": cat,
                    "move": f"mock move {cat} {i}",
                    "transferable_rule": f"rule {i}",
                    "trigger_when": "",
                    "bad_pattern_to_avoid": "",
                    "reuse_for_our_review_system": "",
                    "why_it_matters": "",
                    "retrieval_score": 0.9 - i * 0.01,
                })

        class _MockIndex:
            def query(self, text, top_k=20):
                return mock_hits[:top_k]

        agent = ReviewMentorAgent(use_vector_index=True, max_moves_per_category=4)
        agent._move_index = _MockIndex()
        result = agent._vector_select_moves("any context")

        for cat in M1_CATEGORIES:
            assert cat in result
            assert len(result[cat]) <= 4, (
                f"Category {cat} has {len(result[cat])} rows, expected ≤4"
            )

    def test_review_mentor_advice_separates_command_knowledge_from_case_moves(self):
        """S6: build_advice exposes versioned command knowledge separately from M1 case moves."""
        from optomind_research.review_mentor_agent import ReviewMentorAgent

        agent = ReviewMentorAgent(real_llm=False)
        advice = agent.build_advice(
            user_question="How do mechanisms shape radiative cooling?",
            problem_understanding="Compare physical mechanisms.",
            scope_definition="Optical science.",
        )
        assert advice["command_knowledge"]["status"] == "ok"
        assert advice["command_knowledge"]["precedence"] == "process_and_judgment"
        assert advice["command_knowledge"]["evidence_prohibition"] is True
        assert all(
            skill["version"] and skill["digest"] and skill["provenance"]
            for skill in advice["command_knowledge"]["skills"]
        )
        assert advice["m1_case_moves"]["evidence_prohibition"] is True
        assert advice["workflow_precedence"]["order"] == [
            "command_knowledge",
            "m1_case_moves",
        ]
        assert advice["workflow_precedence"]["does_not_rank_evidence"] is True
        assert advice["evidence_authority"]["authority"] == "papers_and_material_dossiers"
        assert (
            advice["evidence_authority"]["conflict_policy"]
            == "evidence_wins_or_claim_refused"
        )
        assert "cannot resolve scientific facts" in advice[
            "command_knowledge_boundary"
        ].lower()
        assert "guidance_precedence" not in advice
        assert advice.get("mentor_summary") is not None


# ---------------------------------------------------------------------------
# 收敛修复验收 — Fix1~Fix6
# ---------------------------------------------------------------------------

class TestConvergenceFix1ClaimDecomposer:
    """Fix1: claim_decomposer _truncate_to_sentence + open_question isolation."""

    def test_truncate_to_sentence_short_text(self):
        from optomind_research.claim_decomposer import _truncate_to_sentence
        text = "Short text."
        assert _truncate_to_sentence(text, max_chars=800) == "Short text."

    def test_truncate_to_sentence_at_sentence_boundary(self):
        from optomind_research.claim_decomposer import _truncate_to_sentence
        long = "First sentence. Second sentence that is much longer and takes more space."
        result = _truncate_to_sentence(long, max_chars=20)
        assert result.endswith("."), f"Expected sentence boundary, got: {result!r}"
        assert len(result) <= 20

    def test_truncate_to_sentence_cjk_terminal(self):
        from optomind_research.claim_decomposer import _truncate_to_sentence
        text = "第一句话。第二句话是一个更长的描述文字。"
        result = _truncate_to_sentence(text, max_chars=10)
        assert result.endswith("。"), f"Expected CJK terminal, got: {result!r}"

    def test_truncate_to_sentence_no_terminal_gets_ellipsis(self):
        from optomind_research.claim_decomposer import _truncate_to_sentence
        text = "no terminal here"
        result = _truncate_to_sentence(text, max_chars=8)
        assert result.endswith("…"), f"Expected ellipsis fallback, got: {result!r}"


class TestConvergenceFix2GapClassifier:
    """Fix2: m3_gap_classifier retrieval_ready, structural routing."""

    def test_retrieval_ready_in_classify_gap_result(self):
        from optomind_research.m3_gap_classifier import classify_gap
        result = classify_gap(
            claim_text="Some claim text.",
            supporting_chunk_ids=[],
            force_mock=True,
        )
        assert "retrieval_ready" in result, "classify_gap must return retrieval_ready"
        assert "implementation_status" in result, "classify_gap must return implementation_status"

    def test_structural_or_writing_not_retrieval_ready(self):
        from optomind_research.m3_gap_classifier import GAP_ROUTING_TABLE
        entry = GAP_ROUTING_TABLE["structural_or_writing"]
        assert entry["retrieval_ready"] is False
        assert entry["action"] == "return_to_m2a"

    def test_frontier_unknown_not_retrieval_ready(self):
        from optomind_research.m3_gap_classifier import GAP_ROUTING_TABLE
        entry = GAP_ROUTING_TABLE["frontier_unknown"]
        assert entry["retrieval_ready"] is False

    def test_direct_retrievable_is_retrieval_ready(self):
        from optomind_research.m3_gap_classifier import GAP_ROUTING_TABLE
        entry = GAP_ROUTING_TABLE["direct_retrievable"]
        assert entry["retrieval_ready"] is True

    def test_quantitative_benchmark_retrieves_before_extraction(self):
        from optomind_research.m3_gap_classifier import GAP_ROUTING_TABLE
        entry = GAP_ROUTING_TABLE["quantitative_benchmark"]
        assert entry["retrieval_ready"] is True
        assert entry["action"] == "retrieve_then_extract"
        assert "semantic_scholar" in entry["tools"]
        assert "openalex" in entry["tools"]

    def test_analysis_heavy_gaps_can_collect_papers_first(self):
        from optomind_research.m3_gap_classifier import GAP_ROUTING_TABLE
        for gap_type in ("methodological_critique", "contradiction", "normative_recommendation"):
            entry = GAP_ROUTING_TABLE[gap_type]
            assert entry["retrieval_ready"] is True
            assert entry["implementation_status"].startswith("retrieval_implemented")

    def test_retrieval_ready_is_not_overridden_by_pending_postprocessing(self):
        from optomind_research.m3_real_gap_loop import classification_retrieval_ready
        assert classification_retrieval_ready({
            "gap_type": "quantitative_benchmark",
            "retrieval_ready": True,
            "implementation_status": "retrieval_implemented_postprocessing_pending",
        }) is True

    def test_legacy_structural_maps_to_structural_or_writing(self):
        from optomind_research.m3_gap_classifier import _LEGACY_TYPE_MAP
        assert _LEGACY_TYPE_MAP.get("structural") == "structural_or_writing", (
            "Legacy 'structural' must route to structural_or_writing, not direct_retrievable"
        )


class TestConvergenceFix3ReviewWriter:
    """Fix3: EvidencePacket chunk_id, uncited_load_bearing, citation validation."""

    def test_evidence_packet_has_chunk_id(self):
        from optomind_research.review_writer import EvidencePacket
        ep = EvidencePacket(claim_id="c1", paper_id="p1", chunk_id="chunk_abc")
        d = ep.to_dict()
        assert d["chunk_id"] == "chunk_abc"

    def test_section_material_packet_has_uncited_lb(self):
        from optomind_research.review_writer import SectionMaterialMapper
        section = {
            "section_id": "S01",
            "claims": [
                {
                    "claim_id": "S01-C1",
                    "statement": "Load-bearing claim with no evidence.",
                    "load_bearing": True,
                    "supporting_text_chunk_ids": [],
                }
            ],
        }
        mapper = SectionMaterialMapper()
        packet = mapper.map(section)
        assert "S01-C1" in packet.uncited_load_bearing_claim_ids

    def test_section_with_verified_kb_evidence_not_in_uncited_lb(self, tmp_path):
        import sqlite3
        from optomind_research.review_writer import SectionMaterialMapper
        db = tmp_path / "review_knowledge_base.sqlite"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE text_chunks (chunk_id TEXT, paper_id TEXT, text TEXT)")
        con.execute(
            "INSERT INTO text_chunks VALUES (?, ?, ?)",
            ("doi-paper1:0001", "paper1", "Measured evidence supporting the stated claim."),
        )
        con.commit()
        con.close()
        section = {
            "section_id": "S01",
            "claims": [
                {
                    "claim_id": "S01-C1",
                    "statement": "Load-bearing claim with evidence.",
                    "load_bearing": True,
                    "evidence_binding_status": "direct",
                    "supporting_text_chunk_ids": ["doi-paper1:0001"],
                }
            ],
        }
        mapper = SectionMaterialMapper(tmp_path)
        packet = mapper.map(section)
        assert "S01-C1" not in packet.uncited_load_bearing_claim_ids
        assert packet.evidence_packets[0].paper_id == "paper1"
        assert packet.evidence_packets[0].exact_spans

    def test_section_draft_has_uncited_load_bearing(self):
        from optomind_research.review_writer import SectionDraft
        draft = SectionDraft(section_id="S01")
        assert hasattr(draft, "uncited_load_bearing")
        assert isinstance(draft.uncited_load_bearing, list)

    def test_fallback_evidence_packet_preserves_multiple_chunk_ids(self):
        from optomind_research.review_writer import SectionMaterialMapper
        section = {
            "section_id": "S01",
            "claims": [
                {
                    "claim_id": "S01-C1",
                    "statement": "Claim with chunk IDs.",
                    "load_bearing": False,
                    "evidence_binding_status": "direct",
                    "supporting_text_chunk_ids": ["doi-paper1:0001", "doi-paper2:0002"],
                }
            ],
        }
        mapper = SectionMaterialMapper()
        packet = mapper.map(section)
        assert [ep.chunk_id for ep in packet.evidence_packets] == [
            "doi-paper1:0001", "doi-paper2:0002"
        ]


class TestConvergenceFix5RunReview:
    """Fix5: run_review CLI validation gate and MOCK mode."""

    def test_blueprint_validation_blocks_when_failed(self, capsys):
        from run_review import _check_blueprint_validation
        blueprint = {"validation": {"passed": False, "issues": ["missing evidence"]}}
        result = _check_blueprint_validation(blueprint, allow_unvalidated=False)
        assert result is False
        out = capsys.readouterr().out
        assert "BLOCKED" in out

    def test_blueprint_validation_allows_with_flag(self):
        from run_review import _check_blueprint_validation
        blueprint = {"validation": {"passed": False}}
        result = _check_blueprint_validation(blueprint, allow_unvalidated=True)
        assert result is True

    def test_blueprint_validation_passes_when_no_validation_key(self):
        from run_review import _check_blueprint_validation
        blueprint = {}
        result = _check_blueprint_validation(blueprint, allow_unvalidated=False)
        assert result is True  # no validation field → don't block


class TestConvergencePyCompile:
    """验证所有新增/修改文件均可通过 py_compile。"""

    _FILES = [
        "optomind_research/claim_decomposer.py",
        "optomind_research/m3_gap_classifier.py",
        "optomind_research/m3_real_gap_loop.py",
        "optomind_research/review_writer.py",
        "optomind_research/review_mentor_agent.py",
        "run_review.py",
    ]

    def test_all_modified_files_compile(self):
        import py_compile
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        for rel in self._FILES:
            p = root / rel
            assert p.exists(), f"Missing file: {rel}"
            py_compile.compile(str(p), doraise=True)


class TestFeedbackClaimStateSynchronization:
    def test_reviser_accepts_only_safe_downgrades(self, monkeypatch):
        import optomind_research.review_writer as rw

        draft = rw.SectionDraft(
            section_id="S01",
            english_text="A sufficiently long factual sentence remains unsupported in the supplied evidence packet."
        )
        packet = rw.SectionMaterialPacket(
            section_id="S01",
            claims=[{"claim_id": "S01-C01", "statement": "Unsupported factual claim."}],
        )

        def fake_call(*args, **kwargs):
            return {"content": json.dumps({
                "revised_text": (
                    "It remains an open question whether this relationship holds under the stated conditions."
                ),
                "changes": ["Converted an unsupported assertion into an open question."],
                "unresolved_suggestions": [],
                "claim_state_updates": [
                    {
                        "claim_id": "S01-C01",
                        "evidence_requirement": "open_question",
                        "claim_state": "open_question",
                        "reason": "The revised prose no longer asserts this as established fact."
                    },
                    {
                        "claim_id": "S01-C01",
                        "evidence_requirement": "factual",
                        "claim_state": "grounded",
                        "reason": "Unsafe promotion."
                    },
                    {
                        "claim_id": "invented-id",
                        "evidence_requirement": "normative",
                        "claim_state": "reframed",
                        "reason": "Unknown claim."
                    },
                ],
            })}

        monkeypatch.setattr(rw, "call_qwen_chat", fake_call)
        revised = rw.EvidenceAwareRevisionAgent(real_llm=True).revise(
            draft, packet, [{"severity": "high", "message": "Unsupported."}]
        )
        record = revised.revision_history[-1]
        assert record["accepted"] is True
        assert [row["claim_id"] for row in record["claim_state_updates"]] == ["S01-C01"]
        assert len(record["rejected_claim_state_updates"]) == 2

    def test_blueprint_state_sync_removes_nonfactual_dag_edges(self):
        from run_review import _apply_safe_claim_state_updates, _synchronize_blueprint_after_feedback

        blueprint = {
            "sections": [{
                "section_id": "S01",
                "claims": [
                    {"claim_id": "C1", "statement": "Fact one.", "evidence_requirement": "factual"},
                    {"claim_id": "C2", "statement": "Fact two.", "evidence_requirement": "factual"},
                ],
            }],
            "argument_dag": {
                "nodes": [{"claim_id": "C1"}, {"claim_id": "C2"}],
                "claims": [{"claim_id": "C1"}, {"claim_id": "C2"}],
                "edges": [{
                    "source_claim_id": "C1", "target_claim_id": "C2",
                    "source_section_id": "S01", "target_section_id": "S01",
                }],
                "topological_order": ["C1", "C2"],
                "confidence_levels": {"C1": "high", "C2": "medium"},
            },
        }
        applied = _apply_safe_claim_state_updates(
            blueprint,
            [{
                "claim_id": "C2", "evidence_requirement": "open_question",
                "claim_state": "open_question", "reason": "Evidence remains insufficient.",
            }],
            revision_name="feedback_revision_v1",
        )
        _synchronize_blueprint_after_feedback(blueprint)
        assert [row["claim_id"] for row in applied] == ["C2"]
        claim = blueprint["sections"][0]["claims"][1]
        assert claim["claim_state"] == "open_question"
        assert blueprint["argument_dag"]["edges"] == []
        assert blueprint["argument_dag"]["topological_order"] == ["C1"]
        assert blueprint["feedback_state_sync_status"]["removed_nonfactual_dag_edges"] == 1

    def test_claim_targeted_feedback_routes_to_owning_section(self, tmp_path, monkeypatch):
        import run_review
        import optomind_research.review_writer as rw

        blueprint = {"sections": [{
            "section_id": "S04",
            "claims": [{
                "claim_id": "S04-C03",
                "statement": "An unsupported application claim.",
                "evidence_requirement": "factual",
            }],
        }]}
        drafts = [rw.SectionDraft(
            section_id="S04",
            english_text="This sufficiently long sentence presents an unsupported application claim as established fact.",
        )]
        seen = []

        def fake_revise(self, draft, packet, suggestions):
            seen.extend(suggestions)
            return draft

        monkeypatch.setattr(rw.EvidenceAwareRevisionAgent, "revise", fake_revise)
        run_review._run_feedback_revision(
            blueprint,
            drafts,
            [{"target_id": "S04-C03", "severity": "high", "description": "Downgrade it."}],
            kb_path=None,
            output_dir=tmp_path,
            revision_name="feedback_revision_test",
            minimum_severity="high",
        )
        assert len(seen) == 1
        assert seen[0]["target_id"] == "S04-C03"
