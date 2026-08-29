import json
import sqlite3
import sys
from types import SimpleNamespace
from pathlib import Path


def _candidate(**overrides):
    value = {
        "candidate_id": "P-ABSTRACT",
        "doi": "10.1021/acsphotonics.0c00513",
        "title": "Traceable optical paper",
        "year": 2020,
        "venue": "ACS Photonics",
        "source_url": "https://doi.org/10.1021/acsphotonics.0c00513",
        "backends": ["semantic_scholar"],
        "abstract": (
            "Conventional absorption-based colorants cause solar heating, while visible color "
            "absorption reduces cooling. The study establishes a narrowband absorption mitigation "
            "strategy for colored optical surfaces."
        ),
        "llm_relevance_grade": "direct",
    }
    value.update(overrides)
    return value


def _claim(**overrides):
    value = {
        "claim_id": "S01-C01",
        "section_id": "S01",
        "statement": "Conventional absorption-based colorants cause solar heating and visible color absorption reduces cooling.",
        "missing_evidence_components": ["causal relation between color absorption and solar heating"],
        "supporting_text_chunk_ids": [],
        "saturation_score": 0.0,
        "retrieval_query": "absorption-based colorants solar heating cooling",
    }
    value.update(overrides)
    return value


def test_precise_abstract_failure_writes_auditable_abstract_chunk(monkeypatch, tmp_path: Path):
    import optomind_research.m3_kb_ingest as module

    monkeypatch.setattr(module, "download_and_extract", lambda candidate, download_dir=None: ("", ""))
    result = module.KBIngester(kb_sqlite=tmp_path / "kb.sqlite").ingest_oa_candidates(
        [_candidate()], _claim()
    )

    assert result.stats["abstract_fallback_candidates"] == 1
    assert result.stats["abstract_chunks_written"] == 1
    assert result.stats["downloaded"] == 0
    assert result.stats["fulltext_chunks_written"] == 0
    assert result.new_chunk_ids[-1].endswith(":abstract")

    con = sqlite3.connect(tmp_path / "kb.sqlite")
    try:
        row = con.execute(
            "SELECT text,evidence_level,source_kind,provenance_json,raw_json FROM text_chunks"
        ).fetchone()
    finally:
        con.close()
    assert row[0] == _candidate()["abstract"]
    assert row[1:3] == ("abstract", "abstract")
    provenance = json.loads(row[3])
    raw = json.loads(row[4])
    assert provenance["doi"] == "10.1021/acsphotonics.0c00513"
    assert provenance["source"].startswith("https://doi.org/")
    assert raw["original_abstract"] == _candidate()["abstract"]
    assert raw["ingest_source"] == "m3_real_abstract_fallback"


def test_existing_fulltext_prevents_abstract_fallback(monkeypatch, tmp_path: Path):
    import optomind_research.m3_kb_ingest as module

    candidate = _candidate(doi="10.1002/advs.202202061")
    db = tmp_path / "kb.sqlite"
    seed = module.KBIngester(kb_sqlite=db)
    monkeypatch.setattr(
        module,
        "download_and_extract",
        lambda candidate, download_dir=None: ("Background evidence " * 60, "local-fulltext.html"),
    )
    seed.ingest_oa_candidates([candidate], _claim())

    monkeypatch.setattr(module, "download_and_extract", lambda candidate, download_dir=None: ("", ""))
    result = module.KBIngester(kb_sqlite=db).ingest_oa_candidates([candidate], _claim())
    assert result.stats["abstract_chunks_written"] == 0
    assert result.stats["abstract_fallback_skipped_reasons"]["existing_fulltext_chunks"] == 1

    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT count(*) FROM text_chunks WHERE source_kind='abstract'").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM text_chunks WHERE source_kind='fulltext'").fetchone()[0] > 0
    finally:
        con.close()


def test_adjacent_abstract_is_retained_as_method_transfer(monkeypatch, tmp_path: Path):
    import optomind_research.m3_kb_ingest as module

    monkeypatch.setattr(module, "download_and_extract", lambda candidate, download_dir=None: ("", ""))
    db = tmp_path / "kb.sqlite"
    result = module.KBIngester(kb_sqlite=db).ingest_oa_candidates(
        [_candidate(llm_relevance_grade="adjacent")], _claim()
    )
    assert result.stats["abstract_fallback_candidates"] == 1
    assert result.stats["abstract_chunks_written"] == 1
    assert result.method_transfer_chunk_ids
    assert result.factual_candidate_chunk_ids == []
    con = sqlite3.connect(db)
    try:
        source_kind, evidence_level = con.execute(
            "SELECT source_kind,evidence_level FROM text_chunks"
        ).fetchone()
    finally:
        con.close()
    assert source_kind == "method_transfer"
    assert evidence_level == "background"


def test_existing_abstract_fallback_is_not_duplicated(monkeypatch, tmp_path: Path):
    import optomind_research.m3_kb_ingest as module

    monkeypatch.setattr(module, "download_and_extract", lambda candidate, download_dir=None: ("", ""))
    db = tmp_path / "kb.sqlite"
    first = module.KBIngester(kb_sqlite=db).ingest_oa_candidates([_candidate()], _claim())
    second = module.KBIngester(kb_sqlite=db).ingest_oa_candidates([_candidate()], _claim())
    assert first.stats["abstract_chunks_written"] == 1
    assert second.stats["abstract_chunks_written"] == 0
    assert second.stats["abstract_fallback_skipped_reasons"]["abstract_chunk_already_exists"] == 1


def test_claim_focused_selection_prefers_direct_causal_paragraph(monkeypatch, tmp_path: Path):
    import optomind_research.m3_kb_ingest as module

    generic = (
        "Optical colorants are widely investigated in materials science and device platforms. "
        "Their visible appearance and thermal behavior are relevant to many applications, but "
        "this background paragraph does not state the claimed causal relation."
    )
    direct = (
        "Conventional absorption-based colorants cause solar heating because visible color absorption "
        "reduces cooling. Narrowband absorption mitigation preserves the desired visible appearance "
        "while reducing the unwanted solar heat load in the colored surface."
    )
    monkeypatch.setattr(
        module,
        "download_and_extract",
        lambda candidate, download_dir=None: (generic + "\n\n" + direct, "https://example.test/fulltext"),
    )
    result = module.KBIngester(kb_sqlite=tmp_path / "kb.sqlite", min_paragraph_chars=40).ingest_oa_candidates(
        [_candidate(doi="10.1002/advs.202202061")], _claim()
    )

    assert len(result.stats["claim_focused_selection_audit"]) == 1
    selected = result.stats["claim_focused_selection_audit"][0]["final_bound_chunk_ids"]
    assert selected
    con = sqlite3.connect(tmp_path / "kb.sqlite")
    try:
        selected_text = con.execute(
            "SELECT text FROM text_chunks WHERE chunk_id=?", (selected[0],)
        ).fetchone()[0]
    finally:
        con.close()
    assert "cause solar heating" in selected_text
    assert any("exact_phrases" in reason for reason in result.stats["claim_focused_selection_audit"][0]["selection_reasons"][0]["selection_reasons"])


def test_claim_evidence_verifier_marks_abstract_support_provisional(monkeypatch):
    import optomind_research.claim_evidence_verifier as module
    from optomind_research.claim_schema import Claim

    monkeypatch.setattr(
        module,
        "call_qwen_chat",
        lambda *args, **kwargs: {
            "content": json.dumps({
                "bindings": [{
                    "claim_id": "S01-C01",
                    "verdict": "direct",
                    "confidence": "high",
                    "section_fit": "central",
                    "supporting_text_refs": ["T01"],
                    "reason": "The abstract states the causal relation.",
                    "evidence_spans": [{"text_ref": "T01", "quote": "cause solar heating"}],
                }]
            })
        },
    )
    claim = Claim.from_dict({
        "claim_id": "S01-C01",
        "statement": "Conventional absorption-based colorants cause solar heating.",
        "evidence_type": "mechanism",
    })
    verified = module.ClaimEvidenceVerifier().verify_and_bind([claim], {
        "section_id": "S01",
        "candidate_text_chunks": [{
            "chunk_id": "m3gap:paper:abstract",
            "paper_id": "doi:10.1021/acsphotonics.0c00513",
            "doi": "10.1021/acsphotonics.0c00513",
            "title": "Traceable optical paper",
            "text_preview": "Conventional absorption-based colorants cause solar heating.",
            "source_kind": "abstract",
            "evidence_level": "abstract",
            "provenance": {"source": "https://doi.org/10.1021/acsphotonics.0c00513"},
        }],
    })[0]
    assert verified.supporting_text_chunk_ids == ["m3gap:paper:abstract"]
    assert verified.evidence_binding_confidence == "medium"
    assert "abstract_only_evidence_provisional" in verified.critic_flags
    assert verified.saturation_score <= 1.5


def test_abstract_only_readiness_stays_provisional():
    from optomind_research.evidence_readiness_gate import evaluate_evidence_readiness

    readiness = evaluate_evidence_readiness(
        claim_text="Conventional absorption-based colorants cause solar heating.",
        supporting_chunks=[{
            "paper_id": "doi:10.1021/acsphotonics.0c00513",
            "publication_year": 2020,
            "text": "Conventional absorption-based colorants cause solar heating.",
            "source_kind": "abstract",
            "evidence_level": "abstract",
        }],
        claim_type="mechanism",
        binding_status="direct",
        binding_confidence="medium",
    )
    assert readiness["details"]["abstract_only"] is True
    assert readiness["action"] == "supplement"
    assert readiness["readiness_score"] <= 60


def test_pdf_extraction_prefers_fitz_over_pypdf_and_normalizes_text(monkeypatch):
    import optomind_research.m3_kb_ingest as module

    class FakePage:
        def get_text(self):
            return "conventional ﬁlter 8–13 µm with 0.92 emissivity ± 0.01"

    class FakeDocument:
        def __iter__(self):
            return iter([FakePage()])

        def close(self):
            return None

    calls = []

    def fitz_open(**kwargs):
        calls.append(kwargs)
        return FakeDocument()

    class ExplodingPypdf:
        def __getattr__(self, name):
            raise AssertionError("pypdf must not run when fitz succeeds")

    monkeypatch.setitem(sys.modules, "fitz", SimpleNamespace(open=fitz_open))
    monkeypatch.setitem(sys.modules, "pypdf", ExplodingPypdf())

    text = module._extract_text_from_pdf_bytes(b"%PDF-fake")
    assert calls == [{"stream": b"%PDF-fake", "filetype": "pdf"}]
    assert "conventional filter" in text
    assert "8–13 μm" in text
    assert "0.92" in text
    assert "± 0.01" in text


def test_pdf_extraction_uses_pypdf_only_when_fitz_fails(monkeypatch):
    import optomind_research.m3_kb_ingest as module

    class FakePage:
        def extract_text(self):
            return "fallback 532 nm"

    class FakeReader:
        pages = [FakePage()]

    class FakePypdf:
        PdfReader = lambda self, stream: FakeReader()

    monkeypatch.setitem(sys.modules, "fitz", SimpleNamespace(open=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("fitz unavailable"))))
    monkeypatch.setitem(sys.modules, "pypdf", FakePypdf())
    assert module._extract_text_from_pdf_bytes(b"%PDF-fake") == "fallback 532 nm"
