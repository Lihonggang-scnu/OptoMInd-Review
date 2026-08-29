import json
import sqlite3
from pathlib import Path


def _make_kb(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE papers(
            paper_id TEXT PRIMARY KEY, title TEXT, year INTEGER, raw_json TEXT
        );
        CREATE TABLE text_chunks(
            chunk_id TEXT PRIMARY KEY, paper_id TEXT, title TEXT,
            ordinal INTEGER, section_path TEXT, char_start INTEGER,
            char_end INTEGER, text TEXT, raw_json TEXT,
            evidence_level TEXT, source_kind TEXT, content_depth TEXT,
            context_complete INTEGER, use_permission TEXT, scope_fit TEXT,
            discovery_route TEXT, materialization_route TEXT,
            route_provenance_json TEXT, provenance_json TEXT,
            allowed_claim_kinds_json TEXT, relation_roles_json TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO papers VALUES (?,?,?,?)",
        ("p1", "A paper", 2024, "{}"),
    )
    conn.execute(
        """INSERT INTO text_chunks(
            chunk_id,paper_id,title,ordinal,section_path,char_start,char_end,text,
            raw_json,evidence_level,source_kind,content_depth,context_complete,
            use_permission,scope_fit,discovery_route,materialization_route,
            route_provenance_json,provenance_json,allowed_claim_kinds_json,
            relation_roles_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "c1", "p1", "A paper", 7, "Results / Mechanism", 120, 214,
            "The measured optical response follows the stated mechanism.", "{}",
            "fulltext", "fulltext", "fulltext", 1, "factual_support", "direct",
            "semantic_scholar", "s2_fulltext", "{}", "{}", "[]", "[]",
        ),
    )
    conn.commit()
    conn.close()


def test_canonical_graph_preserves_source_locator(tmp_path: Path) -> None:
    from optomind_research.runtime.section_authoring_assets import (
        build_canonical_asset_graph,
    )

    kb = tmp_path / "kb.sqlite"
    _make_kb(kb)
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "paper_id": "p1",
                        "title": "A paper",
                        "year": 2024,
                        "scope_fit": "direct",
                        "acquisition_status": "fulltext",
                        "content_depth": "fulltext",
                        "context_complete": True,
                        "use_permission": "factual_support",
                        "discovery_route": "semantic_scholar",
                        "materialization_route": "s2_fulltext",
                        "canonical_chunk_ids": ["c1"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    graph = build_canonical_asset_graph(
        material_package_path=None,
        source_ledger_path=ledger,
        work_dir=tmp_path,
        kb_paths=[kb],
    )
    chunk = graph.chunks["c1"]
    assert chunk.ordinal == 7
    assert chunk.section_path == "Results / Mechanism"
    assert chunk.char_start == 120
    assert chunk.char_end == 214
    assert chunk.source_locator["section_path"] == "Results / Mechanism"
    assert chunk.source_locator["char_start"] == 120


def test_fulltext_quote_must_exist_before_claim_is_bound(monkeypatch) -> None:
    import optomind_research.claim_evidence_verifier as verifier_module
    from optomind_research.claim_schema import Claim

    def fake_chat(*_args, **_kwargs):
        return {
            "content": json.dumps(
                {
                    "bindings": [
                        {
                            "claim_id": "S01-C01",
                            "verdict": "direct",
                            "confidence": "high",
                            "supporting_text_refs": ["T01"],
                            "evidence_spans": [
                                {
                                    "text_ref": "T01",
                                    "quote": "A fabricated sentence.",
                                }
                            ],
                        }
                    ]
                }
            ),
            "_llm_usage": {"input_tokens": 10, "output_tokens": 10},
        }

    monkeypatch.setattr(verifier_module, "call_qwen_chat", fake_chat)
    claim = Claim(
        "S01-C01",
        "The measured response follows the mechanism.",
        "mechanism",
    )
    result = verifier_module.ClaimEvidenceVerifier(
        model_tier="cheap_model", strict_permissions=True
    ).verify_and_bind(
        [claim],
        {
            "section_id": "S01",
            "candidate_text_chunks": [
                {
                    "chunk_id": "c1",
                    "paper_id": "p1",
                    "normalized_text": "The measured optical response follows the stated mechanism.",
                    "use_permission": "factual_support",
                    "scope_fit": "direct",
                    "content_depth": "fulltext",
                    "context_complete": True,
                    "source_locator": {"section_path": "Results / Mechanism"},
                }
            ],
        },
    )[0]
    assert result.evidence_binding_status == "insufficient"
    assert result.supporting_text_chunk_ids == []
    assert any(flag.startswith("unverified_evidence_spans") for flag in result.critic_flags)


def test_verified_quote_carries_locator_and_relative_offsets(monkeypatch) -> None:
    import optomind_research.claim_evidence_verifier as verifier_module
    from optomind_research.claim_schema import Claim

    def fake_chat(*_args, **_kwargs):
        return {
            "content": json.dumps(
                {
                    "bindings": [
                        {
                            "claim_id": "S01-C01",
                            "verdict": "direct",
                            "confidence": "high",
                            "supporting_text_refs": ["T01"],
                            "evidence_spans": [
                                {
                                    "text_ref": "T01",
                                    "quote": "optical response follows",
                                }
                            ],
                        }
                    ]
                }
            ),
            "_llm_usage": {"input_tokens": 10, "output_tokens": 10},
        }

    monkeypatch.setattr(verifier_module, "call_qwen_chat", fake_chat)
    claim = Claim("S01-C01", "The optical response follows the mechanism.", "mechanism")
    result = verifier_module.ClaimEvidenceVerifier(
        model_tier="cheap_model", strict_permissions=True
    ).verify_and_bind(
        [claim],
        {
            "section_id": "S01",
            "candidate_text_chunks": [
                {
                    "chunk_id": "c1",
                    "paper_id": "p1",
                    "normalized_text": "The measured optical response follows the stated mechanism.",
                    "use_permission": "factual_support",
                    "scope_fit": "direct",
                    "content_depth": "fulltext",
                    "context_complete": True,
                    "source_locator": {
                        "section_path": "Results / Mechanism",
                        "char_start": 120,
                    },
                }
            ],
        },
    )[0]
    assert result.evidence_binding_status == "direct"
    span = result.evidence_spans[0]
    assert span["quote_verified"] is True
    assert span["source_locator"]["section_path"] == "Results / Mechanism"
    assert span["source_locator"]["relative_char_start"] >= 0
