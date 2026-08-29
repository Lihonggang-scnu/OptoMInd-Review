"""Offline tests for dominant-review original-source materialization."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from optomind_research.dominant_review_materialization import (
    run_dominant_review_materialization,
    _dedupe_requests,
    _existing_cache_identity_keys,
    _identity_key,
    _resolve_requests_prefetch,
)


@pytest.fixture()
def mat_tmp() -> Path:
    root = (
        Path(__file__).resolve().parent.parent
        / f"dom-review-mat-tmp-{uuid.uuid4().hex[:8]}"
    )
    os.makedirs(root, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _request(
    reference_number: int,
    title: str,
    *,
    doi: str = "",
    arxiv: str = "",
    s2: str = "",
) -> dict[str, Any]:
    identity = {"title": title}
    if doi:
        identity["doi"] = doi
    if arxiv:
        identity["arxiv_id"] = arxiv
    if s2:
        identity["s2_paper_id"] = s2
    return {
        "reference_number": reference_number,
        "identity": identity,
        "acquisition_priority": "high",
        "acquisition_priority_order": [
            "s2_structured_body",
            "public_oa_fulltext",
            "abstract_claim",
        ],
    }


def _write_requests(mat_tmp: Path, requests: list[dict[str, Any]]) -> Path:
    path = mat_tmp / "ACQUISITION_REQUESTS.json"
    path.write_text(json.dumps({"requests": requests}), encoding="utf-8")
    return path


def _base_cache(mat_tmp: Path, *, units: list[dict[str, Any]] | None = None) -> tuple[Path, Path]:
    units_path = mat_tmp / "base" / "MATERIAL_UNITS_FINAL.json"
    vectors_path = mat_tmp / "base" / "material_vectors.sqlite"
    units_path.parent.mkdir(parents=True, exist_ok=True)
    units_path.write_text(
        json.dumps({"units": units or []}), encoding="utf-8"
    )
    with sqlite3.connect(vectors_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS material_units (unit_id TEXT)")
    return units_path, vectors_path


def _resolved(
    *,
    s2: str,
    title: str,
    doi: str = "",
    abstract: str = "",
) -> dict[str, Any]:
    return {
        "s2_paper_id": s2,
        "doi": doi,
        "arxiv_id": "",
        "title": title,
        "abstract": abstract,
        "year": 2024,
        "venue": "Journal",
        "use_permission": "factual_support",
    }


def _fake_extract_cards(*, packet_path, output_dir, model_tier="b_plus_model", workers=1, skip_existing=True):
    payload = json.loads(Path(packet_path).read_text(encoding="utf-8"))
    cards_dir = Path(output_dir) / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    for packet in payload["packets"]:
        card = {
            "canonical_work_id": packet["canonical_work_id"],
            "question_relevance": "substantial",
            "query_annotation": {
                "query_id": "query:test",
                "question_hash": "sha256:test",
                "model_version": model_tier,
            },
        }
        (cards_dir / f"{packet['canonical_work_id'].replace(':', '_')}.json").write_text(
            json.dumps({"card": card}), encoding="utf-8"
        )
    return {
        "selected_work_count": len(payload["packets"]),
        "new_attempt_count": len(payload["packets"]),
        "reused_count": 0,
        "successful_card_count": len(payload["packets"]),
        "failed_count": 0,
        "rows": [{
            "llm_usage": {
                "success": True,
                "model_name": model_tier,
                "input_tokens": 10,
                "output_tokens": 5,
            }
        } for _ in payload["packets"]],
    }


def _fake_finalize(*, units, cards, question, output_dir, embedder, batch_size=10, workers=4):
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "MATERIAL_UNITS_FINAL.json").write_text(
        json.dumps({"units": [dict(unit) for unit in units]}), encoding="utf-8"
    )
    (out / "material_vectors.sqlite").write_bytes(b"sqlite-placeholder")
    return {
        "unit_count": len(units),
        "card_count": len(cards),
        "embedding_usage": {"input_tokens": 0, "request_count": 0},
        "vector_result": {"requested": len(units), "reused": 0, "embedded": 0},
        "final_units_path": str(out / "MATERIAL_UNITS_FINAL.json"),
        "vector_cache_path": str(out / "material_vectors.sqlite"),
    }


def _fake_merge(**kwargs):
    output_root = Path(kwargs["output_root"])
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "MERGE_REPORT.json").write_text(
        json.dumps({"status": "completed"}), encoding="utf-8"
    )
    return {"status": "completed", "output_root": str(output_root)}


class _ResumableCardExtractor:
    """Fake extractor that can leave one card missing until resumed."""

    def __init__(self, missing_indexes: set[int]) -> None:
        self.missing_indexes = set(missing_indexes)
        self.calls = 0
        self.written: list[str] = []
        self.reused: list[str] = []

    def __call__(
        self,
        *,
        packet_path: Path,
        output_dir: Path,
        model_tier: str = "b_plus_model",
        workers: int = 1,
        skip_existing: bool = True,
    ) -> dict[str, Any]:
        payload = json.loads(Path(packet_path).read_text(encoding="utf-8"))
        cards_dir = Path(output_dir) / "cards"
        cards_dir.mkdir(parents=True, exist_ok=True)
        rows: list[dict[str, Any]] = []
        written_now: list[str] = []
        reused_now: list[str] = []
        self.calls += 1
        for index, packet in enumerate(payload["packets"]):
            work = str(packet["canonical_work_id"])
            path = cards_dir / f"{work.replace(':', '_')}.json"
            if skip_existing and path.exists():
                value = json.loads(path.read_text(encoding="utf-8"))
                audit = value.get("audit") or {}
                usage = audit.get("llm_usage") or {}
                rows.append({
                    "canonical_work_id": work,
                    "status": "reused",
                    "card_path": str(path),
                    "llm_usage": usage,
                    "per_attempt_usage": usage.get("per_attempt_usage") or [],
                    "format_retry_count": usage.get("format_retry_count") or 0,
                })
                reused_now.append(work)
                self.reused.append(work)
                continue
            if index in self.missing_indexes:
                continue
            card = {
                "schema_version": "optomind.material_proposition_card.v1",
                "canonical_work_id": work,
                "query_annotation": {"model_version": model_tier},
                "question_relevance": "substantial",
                "paper_functions": ["reported_result"],
                "propositions": [],
                "background_contexts": [],
                "emergent_axis_candidates": [],
                "seed_axis_assignments": [],
            }
            usage = {
                "model_name": model_tier,
                "input_tokens": 10,
                "output_tokens": 5,
                "model_call_count": 1,
                "format_retry_count": 0,
                "per_attempt_usage": [{
                    "model_name": model_tier,
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "success": True,
                }],
            }
            audit = {"status": "passed", "llm_usage": usage}
            path.write_text(
                json.dumps({"card": card, "audit": audit}),
                encoding="utf-8",
            )
            rows.append({
                "canonical_work_id": work,
                "status": "passed",
                "card_path": str(path),
                "llm_usage": usage,
                "per_attempt_usage": usage["per_attempt_usage"],
                "format_retry_count": 0,
            })
            written_now.append(work)
            self.written.append(work)
        return {
            "selected_work_count": len(payload["packets"]),
            "new_attempt_count": len(written_now),
            "reused_count": len(reused_now),
            "successful_card_count": len(written_now) + len(reused_now),
            "failed_count": 0,
            "rows": rows,
        }


def _s2_chunk(request: Mapping[str, Any], resolved: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [{
        "chunk_id": f"s2-body:{resolved['s2_paper_id']}:0000",
        "paper_id": resolved["s2_paper_id"],
        "doi": resolved.get("doi", ""),
        "title": resolved.get("title", ""),
        "text": f"Structured body snippet for {request['reference_number']}.",
        "section_path": "results",
        "source_kind": "s2_body",
        "content_depth": "fulltext",
        "use_permission": "factual_support",
        "provenance": {"route": "s2_structured_body"},
    }]


def _abstract_chunk(request: Mapping[str, Any], resolved: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not resolved.get("abstract"):
        return []
    return [{
        "chunk_id": f"abstract:{resolved['s2_paper_id']}:0000",
        "paper_id": resolved["s2_paper_id"],
        "doi": resolved.get("doi", ""),
        "title": resolved.get("title", ""),
        "text": resolved["abstract"],
        "section_path": "abstract",
        "source_kind": "true_abstract",
        "content_depth": "abstract_claim",
        "use_permission": "contextual_or_qualified_support",
        "provenance": {"route": "abstract_claim"},
    }]


def _run(mat_tmp: Path, requests: list[dict[str, Any]], **overrides):
    base_units, base_vectors = _base_cache(mat_tmp)
    review_identity = {"paper_id": "review:paper", "title": "Review"}
    return run_dominant_review_materialization(
        acquisition_requests_path=_write_requests(mat_tmp, requests),
        base_units_path=base_units,
        base_vectors_path=base_vectors,
        output_dir=mat_tmp / "out",
        question="Which originals support the review?",
        review_identity=review_identity,
        extract_cards_fn=_fake_extract_cards,
        finalize_fn=_fake_finalize,
        merge_fn=_fake_merge,
        **overrides,
    )


def test_exact_id_path_and_route_precedence(mat_tmp: Path) -> None:
    requests = [
        _request(1, "Paper one", doi="10.1/one"),
        _request(2, "Paper two", s2="S2:TWO"),
    ]
    resolved_map = {
        1: _resolved(s2="S2:ONE", title="Paper one", doi="10.1/one",
                     abstract="Abstract one."),
        2: _resolved(s2="S2:TWO", title="Paper two", abstract=""),
    }
    resolve_identity_fn = lambda request: (
        (resolved_map[int(request["reference_number"])], "exact_identity")
        if request["reference_number"] in (1, 2)
        else (None, "unresolved")
    )

    def s2_provider(request, resolved):
        return _s2_chunk(request, resolved)

    def abstract_provider(request, resolved):
        return _abstract_chunk(request, resolved)

    report = _run(
        mat_tmp,
        requests,
        resolve_identity_fn=resolve_identity_fn,
        route_providers={
            "s2_structured_body": s2_provider,
            "abstract_claim": abstract_provider,
        },
    )
    assert report["request_total"] == 2
    assert report["route_counts"] == {"s2_structured_body": 2}
    assert report["unit_count"] == 2
    assert report["per_request_audit"][0]["route"] == "s2_structured_body"
    assert report["unresolved_requests"] == []

    # Request with a stable DOI resolves via exact identity, no title search.
    assert report["per_request_audit"][0]["resolution_mode"] == "exact_identity"


def test_abstract_fallback_used_when_body_empty(mat_tmp: Path) -> None:
    requests = [_request(1, "Paper one", s2="S2:ONE")]
    resolved_map = {
        1: _resolved(s2="S2:ONE", title="Paper one", abstract="Abstract one."),
    }
    report = _run(
        mat_tmp,
        requests,
        resolve_identity_fn=lambda request: (
            (resolved_map[1], "exact_identity")
        ),
        route_providers={
            "s2_structured_body": lambda request, resolved: [],
            "abstract_claim": _abstract_chunk,
        },
    )
    assert report["route_counts"] == {"abstract_claim": 1}
    assert report["per_request_audit"][0]["route"] == "abstract_claim"


def test_title_fallback_only_when_no_stable_id(mat_tmp: Path) -> None:
    requests = [
        _request(1, "Stable paper", doi="10.1/stable"),
        _request(2, "Title-only paper"),
    ]
    stable_ids_calls: list[list[str]] = []
    search_calls: list[str] = []
    resolved_map = {
        1: _resolved(s2="S2:ONE", title="Stable paper", doi="10.1/stable"),
        2: _resolved(s2="S2:TWO", title="Title-only paper"),
    }

    def resolve_identity_fn(request):
        identity = request.get("identity") or {}
        if identity.get("doi"):
            stable_ids_calls.append(["DOI:" + identity["doi"]])
            return resolved_map[1], "exact_identity"
        search_calls.append(identity.get("title", ""))
        return resolved_map[2], "exact_title_fallback"

    report = _run(
        mat_tmp,
        requests,
        resolve_identity_fn=resolve_identity_fn,
        route_providers={
            "s2_structured_body": _s2_chunk,
            "abstract_claim": lambda request, resolved: [],
        },
    )
    assert stable_ids_calls == [["DOI:10.1/stable"]]
    assert search_calls == ["Title-only paper"]
    modes = {
        row["reference_number"]: row["resolution_mode"]
        for row in report["per_request_audit"]
    }
    assert modes["1"] == "exact_identity"
    assert modes["2"] == "exact_title_fallback"


def test_no_cap_all_requests_processed(mat_tmp: Path) -> None:
    requests = [_request(index, f"Paper {index}") for index in range(1, 41)]
    resolved_map = {
        index: _resolved(s2=f"S2:{index}", title=f"Paper {index}")
        for index in range(1, 41)
    }
    report = _run(
        mat_tmp,
        requests,
        resolve_identity_fn=lambda request: (
            (resolved_map[int(request["reference_number"])], "exact_identity")
        ),
        route_providers={
            "s2_structured_body": _s2_chunk,
            "abstract_claim": lambda request, resolved: [],
        },
    )
    assert report["kept_request_count"] == 40
    assert report["unit_count"] == 40
    assert report["route_counts"] == {"s2_structured_body": 40}


def test_cache_dedupe(mat_tmp: Path) -> None:
    existing = [{
        "unit_id": "unit:text:existing",
        "work_id": "work:existing",
        "identity": {
            "chunk_id": "existing:0000",
            "paper_id": "S2:EXISTING",
            "doi": "10.1/existing",
            "title": "Existing paper",
        },
        "durable_content": {"raw_text": "existing"},
    }]
    base_units, base_vectors = _base_cache(mat_tmp, units=existing)
    requests = [_request(1, "Existing paper", doi="10.1/existing")]
    report = run_dominant_review_materialization(
        acquisition_requests_path=_write_requests(mat_tmp, requests),
        base_units_path=base_units,
        base_vectors_path=base_vectors,
        output_dir=mat_tmp / "out",
        question="q",
        review_identity={"paper_id": "r", "title": "R"},
        extract_cards_fn=_fake_extract_cards,
        finalize_fn=_fake_finalize,
        merge_fn=_fake_merge,
    )
    assert report["kept_request_count"] == 0
    assert report["unit_count"] == 0
    assert report["dedupe_audit"][0]["status"] == "deduped_existing_cache"


def test_no_overwrite(mat_tmp: Path) -> None:
    output_dir = mat_tmp / "existing_out"
    output_dir.mkdir()
    base_units, base_vectors = _base_cache(mat_tmp)
    with pytest.raises(FileExistsError):
        run_dominant_review_materialization(
            acquisition_requests_path=_write_requests(mat_tmp, []),
            base_units_path=base_units,
            base_vectors_path=base_vectors,
            output_dir=output_dir,
            question="q",
            review_identity={},
            extract_cards_fn=_fake_extract_cards,
            finalize_fn=_fake_finalize,
            merge_fn=_fake_merge,
        )


def test_merge_output_and_increment(mat_tmp: Path) -> None:
    requests = [_request(1, "Paper one", s2="S2:ONE")]
    resolved = _resolved(s2="S2:ONE", title="Paper one", abstract="Abstract one.")
    report = _run(
        mat_tmp,
        requests,
        resolve_identity_fn=lambda request: (resolved, "exact_identity"),
        route_providers={
            "s2_structured_body": _s2_chunk,
            "abstract_claim": lambda request, resolved: [],
        },
    )
    increment = mat_tmp / "out" / "task_local_increment"
    assert (increment / "MATERIAL_UNITS_FINAL.json").exists()
    assert (increment / "material_vectors.sqlite").exists()
    merged = mat_tmp / "out" / "merged_cache_snapshot"
    assert (merged / "MERGE_REPORT.json").exists()
    assert report["merged_snapshot_path"] == str(merged)
    assert report["finalization"]["unit_count"] == 1


def test_unresolved_requests_do_not_block_successes(mat_tmp: Path) -> None:
    requests = [
        _request(1, "Good paper", s2="S2:GOOD"),
        _request(2, "Bad paper", s2="S2:BAD"),
    ]
    resolved = _resolved(s2="S2:GOOD", title="Good paper")

    def resolve_identity_fn(request):
        if int(request["reference_number"]) == 2:
            return None, "unresolved"
        return resolved, "exact_identity"

    report = _run(
        mat_tmp,
        requests,
        resolve_identity_fn=resolve_identity_fn,
        route_providers={
            "s2_structured_body": _s2_chunk,
            "abstract_claim": lambda request, resolved: [],
        },
    )
    assert report["unit_count"] == 1
    assert report["unresolved_requests"] == [{
        "identity_key": "s2:S2:BAD",
        "reference_number": "2",
        "status": "unresolved",
        "resolution_mode": "unresolved",
        "service_unavailable": False,
    }]


def test_runner_help_and_live_gate_network_free() -> None:
    import scripts.run_dominant_review_materialization_real as runner

    with pytest.raises(SystemExit) as exc:
        runner.main(["--help"])
    assert exc.value.code == 0
    with pytest.raises(SystemExit) as exc:
        runner.main([])
    assert exc.value.code == 2


class _FakeS2Record:
    """Minimal stand-in for S2PaperRecord used by the default OA path."""

    def __init__(
        self,
        paper_id: str,
        title: str,
        *,
        doi: str = "",
        arxiv: str = "",
        abstract: str = "",
    ) -> None:
        self.paper_id = paper_id
        self.corpus_id = None
        self.doi = doi
        self.title = title
        self.abstract = abstract
        self.year = 2024
        self.venue = "Journal"
        self.is_oa = True
        self.s2_open_access_candidate_url = (
            f"https://example.org/oa/{paper_id}.pdf"
        )
        self.influential_citation_count = 5
        self.external_ids = {"DOI": doi, "ArXiv": arxiv}
        self.use_permission = "factual_support"


class _FakeAcquisitionResult:
    def __init__(self, chunk_count: int = 1) -> None:
        self.selected_paper_ids = ["S2:OA"]
        self.skipped: list[dict[str, Any]] = []
        self.new_chunk_ids = [f"oa:S2:OA:0000"] * max(0, chunk_count)
        self.reused_chunk_ids: list[str] = []
        self.new_paper_ids = ["S2:OA"]
        self.stats = {
            "attempted": 1,
            "downloaded": 1,
            "parse_failed": 0,
            "resolver_waves": 1,
            "abstracts_enriched": 0,
            "abstract_enrichment_sources": {},
            "paper_outcomes": [{
                "paper_id": "S2:OA",
                "title": "OA paper",
                "doi": "10.1/oa",
                "status": "oa_fulltext_success",
                "success_wave": "s2_direct",
                "route_count": 1,
                "new_chunk_count": chunk_count,
                "reused_chunk_count": 0,
                "materialized_chunk_count": chunk_count,
                "abstract_enriched_source": "",
                "visual_ingest": {
                    "status": "visual_ok",
                    "eligible_visual_chunks": 1,
                },
                "visual_candidate_count": 1,
                "waves": [{
                    "wave": "s2_direct",
                    "resolver_status": "resolved",
                    "route_count": 1,
                    "url_attempts": ["https://example.org/oa/S2:OA.pdf"],
                }],
            }],
            "successful_papers": 1,
            "visual_candidate_count": 1,
            "visual_ingest_status_counts": {"visual_ok": 1},
            "non_body_chunks_quarantined": 0,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_paper_ids": self.selected_paper_ids,
            "skipped": self.skipped,
            "new_chunk_ids": self.new_chunk_ids,
            "reused_chunk_ids": self.reused_chunk_ids,
            "new_paper_ids": self.new_paper_ids,
            "stats": self.stats,
        }


class _FakeOAAcquirer:
    def __init__(
        self,
        *,
        result: _FakeAcquisitionResult | None = None,
    ) -> None:
        self.kb_sqlite: Path | None = None
        self.download_dir: Path | None = None
        self.result = result or _FakeAcquisitionResult()
        self.acquire_calls: list[dict[str, Any]] = []

    def __call__(self, kb_sqlite: Path, download_dir: Path):
        self.kb_sqlite = Path(kb_sqlite)
        self.download_dir = Path(download_dir)
        return self

    def acquire(
        self,
        selections,
        *,
        max_successes: int = 1,
        source_task_id: str = "s2_fulltext_escalation",
    ):
        self.acquire_calls.append({
            "selections": selections,
            "max_successes": max_successes,
            "source_task_id": source_task_id,
        })
        return self.result


def _fake_create_kb(path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _oa_chunk(paper_id: str = "S2:OA") -> list[dict[str, Any]]:
    return [{
        "chunk_id": f"oa:{paper_id}:0000",
        "paper_id": paper_id,
        "doi": "10.1/oa",
        "title": "OA paper",
        "text": "Full text paragraph from public OA.",
        "section_path": "methods",
        "source_kind": "oa_fulltext",
        "content_depth": "fulltext",
        "use_permission": "factual_support",
        "context_complete": True,
        "allowed_claim_kinds": [],
        "provenance": {"route": "public_oa_fulltext"},
    }]


def _oa_chunk_for(
    paper_id: str,
    *,
    title: str = "OA paper",
    doi: str = "10.1/oa",
) -> list[dict[str, Any]]:
    return [{
        "chunk_id": f"oa:{paper_id}:0000",
        "paper_id": paper_id,
        "doi": doi,
        "title": title,
        "text": f"OA full text for {paper_id}.",
        "section_path": "methods",
        "source_kind": "oa_fulltext",
        "content_depth": "fulltext",
        "use_permission": "factual_support",
        "context_complete": True,
        "allowed_claim_kinds": [],
        "provenance": {"route": "public_oa_fulltext"},
    }]


def test_oa_default_provider_runs_between_s2_and_abstract(mat_tmp: Path) -> None:
    requests = [_request(1, "OA paper", doi="10.1/oa")]
    paper = _FakeS2Record("S2:OA", "OA paper", doi="10.1/oa", abstract="Abstract.")
    resolved = _resolved(
        s2="S2:OA", title="OA paper", doi="10.1/oa", abstract="Abstract."
    )
    resolved["s2_record"] = paper
    abstract_calls: list[str] = []
    acquirer = _FakeOAAcquirer()
    read_paper_ids: list[str] = []

    def read_oa(kb_sqlite: Path, paper_id: str):
        read_paper_ids.append(paper_id)
        return _oa_chunk(paper_id)

    report = _run(
        mat_tmp,
        requests,
        resolve_identity_fn=lambda request: (resolved, "exact_identity"),
        route_providers={
            "s2_structured_body": lambda request, resolved: [],
            "abstract_claim": (
                lambda request, resolved: (
                    abstract_calls.append("abstract"),
                    _abstract_chunk(request, resolved),
                )[1]
            ),
        },
        oa_acquirer_factory=acquirer,
        oa_create_kb_fn=_fake_create_kb,
        oa_read_chunks_fn=read_oa,
    )
    assert report["route_counts"] == {"public_oa_fulltext": 1}
    assert abstract_calls == []
    assert report["unit_count"] == 1
    assert read_paper_ids == ["S2:OA"]
    assert acquirer.kb_sqlite is not None
    assert acquirer.download_dir is not None
    assert Path(acquirer.kb_sqlite).as_posix().endswith(
        "task_local_increment/oa_runtime/runtime_kb.sqlite"
    )
    assert len(acquirer.acquire_calls) == 1
    assert acquirer.acquire_calls[0]["max_successes"] == 1
    assert (
        acquirer.acquire_calls[0]["source_task_id"]
        == "dominant_review_materialization"
    )
    units = json.loads(
        (
            mat_tmp / "out" / "task_local_increment"
            / "MATERIAL_UNITS_FINAL.json"
        ).read_text(encoding="utf-8")
    )["units"]
    assert units[0]["material_class"] == "oa_fulltext"
    assert units[0]["durable_content"]["content_depth"] == "fulltext"
    quality = units[0]["durable_content_card"]["content_quality"]
    assert quality["source_kind"] == "oa_fulltext"
    assert quality["evidence_ceiling"] == "factual_support"
    assert report["oa_audits"][0]["chunk_count"] == 1
    assert report["oa_audits"][0]["decision"]["desired_assets"] == [
        "complete_text"
    ]
    outcome = (
        report["oa_audits"][0]["acquisition"]["stats"]["paper_outcomes"][0]
    )
    assert outcome["success_wave"] == "s2_direct"
    assert outcome["visual_candidate_count"] == 1
    assert report["cards_model_tier"] == "b_plus_model"


def test_oa_failure_falls_to_true_abstract(mat_tmp: Path) -> None:
    requests = [_request(1, "OA paper", doi="10.1/oa")]
    paper = _FakeS2Record("S2:OA", "OA paper", doi="10.1/oa", abstract="Abstract.")
    resolved = _resolved(
        s2="S2:OA", title="OA paper", doi="10.1/oa", abstract="Abstract."
    )
    resolved["s2_record"] = paper
    acquirer = _FakeOAAcquirer(result=_FakeAcquisitionResult(chunk_count=0))
    abstract_calls: list[str] = []

    report = _run(
        mat_tmp,
        requests,
        resolve_identity_fn=lambda request: (resolved, "exact_identity"),
        route_providers={
            "s2_structured_body": lambda request, resolved: [],
            "abstract_claim": (
                lambda request, resolved: (
                    abstract_calls.append("abstract"),
                    _abstract_chunk(request, resolved),
                )[1]
            ),
        },
        oa_acquirer_factory=acquirer,
        oa_create_kb_fn=_fake_create_kb,
        oa_read_chunks_fn=lambda kb_sqlite, paper_id: [],
    )
    assert report["route_counts"] == {"abstract_claim": 1}
    assert abstract_calls == ["abstract"]
    assert report["oa_audits"][0]["chunk_count"] == 0
    units = json.loads(
        (
            mat_tmp / "out" / "task_local_increment"
            / "MATERIAL_UNITS_FINAL.json"
        ).read_text(encoding="utf-8")
    )["units"]
    assert units[0]["material_class"] == "abstract_claim"
    assert (
        units[0]["durable_content_card"]["content_quality"]["source_kind"]
        == "true_abstract"
    )


def test_production_oa_default_instantiates_real_acquirer(
    mat_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import optomind_research.s2_fulltext_acquisition as s2fta

    instances: list[Any] = []

    class PatchedS2FulltextAcquirer:
        def __init__(self, *, kb_sqlite, download_dir):
            self.kb_sqlite = Path(kb_sqlite)
            self.download_dir = Path(download_dir)
            self.acquire_calls: list[tuple[Any, int, str]] = []
            instances.append(self)

        def acquire(self, selections, *, max_successes=1, source_task_id=""):
            self.acquire_calls.append(
                (selections, max_successes, source_task_id)
            )
            return _FakeAcquisitionResult()

    monkeypatch.setattr(s2fta, "S2FulltextAcquirer", PatchedS2FulltextAcquirer)
    decision_calls: list[tuple[Any, bool]] = []
    original_decision = s2fta.decide_fulltext_escalation

    def spy_decision(paper, *, role_labels=(), need_visual_assets=False,
                     need_complete_context=False):
        decision_calls.append((paper, need_complete_context))
        return original_decision(
            paper,
            role_labels=role_labels,
            need_visual_assets=need_visual_assets,
            need_complete_context=need_complete_context,
        )

    monkeypatch.setattr(s2fta, "decide_fulltext_escalation", spy_decision)

    requests = [_request(1, "OA paper", doi="10.1/oa")]
    paper = _FakeS2Record("S2:OA", "OA paper", doi="10.1/oa", abstract="Abstract.")
    resolved = _resolved(
        s2="S2:OA", title="OA paper", doi="10.1/oa", abstract="Abstract."
    )
    resolved["s2_record"] = paper
    report = _run(
        mat_tmp,
        requests,
        resolve_identity_fn=lambda request: (resolved, "exact_identity"),
        route_providers={
            "s2_structured_body": lambda request, resolved: [],
            "abstract_claim": lambda request, resolved: [],
        },
        oa_read_chunks_fn=lambda kb_sqlite, paper_id: _oa_chunk(paper_id),
    )
    assert report["route_counts"] == {"public_oa_fulltext": 1}
    assert len(instances) == 1
    assert Path(instances[0].kb_sqlite).as_posix().endswith(
        "task_local_increment/oa_runtime/runtime_kb.sqlite"
    )
    assert Path(instances[0].download_dir).as_posix().endswith(
        "task_local_increment/oa_downloads"
    )
    assert len(instances[0].acquire_calls) == 1
    selections, max_successes, source_task_id = instances[0].acquire_calls[0]
    assert max_successes == 1
    assert source_task_id == "dominant_review_materialization"
    assert selections[0][0] is paper
    assert selections[0][1].desired_assets == ["complete_text"]
    assert decision_calls == [(paper, True)]


def test_prefetch_resolution_batches_120_and_501(mat_tmp: Path) -> None:
    def run_case(count: int):
        requests = [
            _request(index, f"Paper {index}", doi=f"10.1/{index}")
            for index in range(1, count + 1)
        ]
        batch_sizes: list[int] = []
        search_calls: list[str] = []

        def batch_fn(ids):
            batch_sizes.append(len(ids))
            records = []
            for raw in ids:
                if raw.startswith("DOI:"):
                    doi = raw[4:]
                    records.append(
                        _FakeS2Record(f"S2:{doi}", f"Paper {doi}", doi=doi)
                    )
            return records, None

        def search_fn(title):
            search_calls.append(title)
            return []

        _, audit = _resolve_requests_prefetch(
            requests,
            batch_fn=batch_fn,
            search_fn=search_fn,
        )
        return audit, batch_sizes, search_calls

    audit120, sizes120, search120 = run_case(120)
    assert audit120["batch_call_count"] == 1
    assert sizes120 == [120]
    assert audit120["stable_id_count"] == 120
    assert audit120["resolved_count"] == 120
    assert search120 == []

    audit501, sizes501, _ = run_case(501)
    assert audit501["batch_call_count"] == 2
    assert sizes501 == [500, 1]
    assert audit501["stable_id_count"] == 501
    assert audit501["resolved_count"] == 501


def test_run_uses_prefetch_resolution_single_batch(mat_tmp: Path) -> None:
    count = 120
    requests = [
        _request(index, f"Paper {index}", doi=f"10.1/{index}")
        for index in range(1, count + 1)
    ]
    batch_sizes: list[int] = []
    search_calls: list[str] = []

    def batch_fn(ids):
        batch_sizes.append(len(ids))
        records = []
        for raw in ids:
            if raw.startswith("DOI:"):
                doi = raw[4:]
                records.append(
                    _FakeS2Record(f"S2:{doi}", f"Paper {doi}", doi=doi)
                )
        return records, None

    def search_fn(title):
        search_calls.append(title)
        return []

    report = _run(
        mat_tmp,
        requests,
        batch_fn=batch_fn,
        search_fn=search_fn,
        route_providers={
            "s2_structured_body": _s2_chunk,
            "abstract_claim": lambda request, resolved: [],
        },
    )
    assert report["kept_request_count"] == count
    assert report["unit_count"] == count
    assert report["resolution_audit"]["batch_call_count"] == 1
    assert report["resolution_audit"]["stable_id_count"] == count
    assert report["resolution_audit"]["resolved_count"] == count
    assert report["resolution_audit"]["title_call_count"] == 0
    assert batch_sizes == [count]
    assert search_calls == []


def test_title_fallback_only_unresolved_and_threshold(mat_tmp: Path) -> None:
    requests = [
        _request(1, "Resolved paper", doi="10.1/resolved"),
        _request(2, "Title only paper"),
        _request(3, "Stable missing", doi="10.1/missing"),
    ]
    batch_calls: list[list[str]] = []
    search_calls: list[str] = []

    def batch_fn(ids):
        batch_calls.append(list(ids))
        records = []
        for raw in ids:
            if raw == "DOI:10.1/resolved":
                records.append(
                    _FakeS2Record(
                        "S2:RESOLVED", "Resolved paper", doi="10.1/resolved"
                    )
                )
        return records, None

    def search_fn(title):
        search_calls.append(title)
        if title == "Title only paper":
            return [
                _FakeS2Record("S2:TITLE", "Title only paper", doi="10.1/title")
            ]
        if title == "Stable missing":
            return [
                _FakeS2Record("S2:MISSING", "Stable missing", doi="10.1/missing")
            ]
        return []

    results, audit = _resolve_requests_prefetch(
        requests,
        batch_fn=batch_fn,
        search_fn=search_fn,
    )
    assert batch_calls == [["DOI:10.1/resolved", "DOI:10.1/missing"]]
    assert search_calls == ["Title only paper", "Stable missing"]
    assert audit["batch_call_count"] == 1
    assert audit["title_call_count"] == 2
    assert audit["resolved_count"] == 3
    assert audit["title_resolved_count"] == 2
    assert [row["resolution_mode"] for row in results] == [
        "exact_identity",
        "exact_title_fallback",
        "exact_title_fallback",
    ]


def test_identity_key_normalization_dedupe(mat_tmp: Path) -> None:
    existing = [{
        "unit_id": "unit:text:existing",
        "work_id": "work:existing",
        "identity": {
            "paper_id": "S2:EXISTING",
            "doi": "https://doi.org/10.1000/XYZ",
            "title": "Existing paper",
            "locator": {"arxiv_id": "https://arxiv.org/abs/2606.21945"},
        },
        "durable_content": {"raw_text": "existing"},
    }]
    requests = [
        _request(1, "Existing paper", doi="doi:10.1000/xyz"),
        _request(2, "Existing arXiv", arxiv="arXiv:2606.21945"),
        _request(3, "New paper"),
    ]
    kept, audit = _dedupe_requests(
        requests, _existing_cache_identity_keys(existing)
    )
    assert [row["reference_number"] for row in kept] == [3]
    assert [row["status"] for row in audit] == [
        "deduped_existing_cache",
        "deduped_existing_cache",
    ]


def test_no_route_cap_and_base_cache_untouched(mat_tmp: Path) -> None:
    count = 130
    requests = [
        _request(index, f"Paper {index}") for index in range(1, count + 1)
    ]
    resolved_map = {
        index: _resolved(s2=f"S2:{index}", title=f"Paper {index}")
        for index in range(1, count + 1)
    }
    base_units, base_vectors = _base_cache(mat_tmp)
    before = (base_units.read_bytes(), base_vectors.read_bytes())
    report = run_dominant_review_materialization(
        acquisition_requests_path=_write_requests(mat_tmp, requests),
        base_units_path=base_units,
        base_vectors_path=base_vectors,
        output_dir=mat_tmp / "out",
        question="Which originals support the review?",
        review_identity={"paper_id": "review:paper", "title": "Review"},
        extract_cards_fn=_fake_extract_cards,
        finalize_fn=_fake_finalize,
        merge_fn=_fake_merge,
        resolve_identity_fn=lambda request: (
            resolved_map[int(request["reference_number"])], "exact_identity"
        ),
        route_providers={
            "s2_structured_body": _s2_chunk,
            "abstract_claim": lambda request, resolved: [],
        },
    )
    assert report["kept_request_count"] == count
    assert report["unit_count"] == count
    assert report["route_counts"] == {"s2_structured_body": count}
    assert report["no_admission_cap"] is True
    assert report["quota_class"] == "dominant_source_unbundling_non_quota"
    assert base_units.read_bytes() == before[0]
    assert base_vectors.read_bytes() == before[1]


def test_cli_help_from_repo_root_network_free() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    script = repo_root / "scripts" / "run_dominant_review_materialization_real.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    assert "--live" in result.stdout
    assert "--requests" in result.stdout


def test_enriched_identity_reuse_offline(mat_tmp: Path) -> None:
    requests = [{
        "reference_number": 85,
        "identity": {
            "doi": "10.1/enriched",
            "arxiv_id": "",
            "title": "Enriched original",
            "authors": "A. Author",
            "year": "2024",
            "s2_paper_id": "S2:ENRICHED",
            "openalex_id": "W123",
        },
        "enriched": {
            "title": "Enriched original",
            "abstract": "True abstract of the original.",
            "s2_paper_id": "S2:ENRICHED",
            "openalex_id": "W123",
            "doi": "10.1/enriched",
            "arxiv_id": "",
            "year": "2024",
            "authors": ["A. Author"],
            "venue": "Journal",
            "external_ids": {"DOI": "10.1/enriched", "OpenAlex": "W123"},
            "open_access_url": "https://example.org/oa/enriched.pdf",
        },
        "acquisition_priority": "high",
        "useful_axes": ["axis-1", "axis-2"],
        "useful_sections": ["section-2"],
        "likely_evidence_roles": ["central_fact"],
        "reason": "kept by screening",
    }]
    batch_calls: list[Any] = []
    search_calls: list[str] = []
    acquirer = _FakeOAAcquirer()

    report = _run(
        mat_tmp,
        requests,
        batch_fn=lambda ids: (
            batch_calls.append(list(ids)) or ([], None)
        ),
        search_fn=lambda title: (
            search_calls.append(title) or []
        ),
        route_providers={
            "s2_structured_body": lambda request, resolved: [],
            "abstract_claim": lambda request, resolved: [],
        },
        oa_acquirer_factory=acquirer,
        oa_create_kb_fn=_fake_create_kb,
        oa_read_chunks_fn=(
            lambda kb_sqlite, paper_id: _oa_chunk_for(
                paper_id, title="Enriched original", doi="10.1/enriched"
            )
        ),
    )
    assert batch_calls == []
    assert search_calls == []
    assert report["route_counts"] == {"public_oa_fulltext": 1}
    row = report["per_request_audit"][0]
    assert row["resolution_mode"] == "enriched_identity"
    assert row["service_unavailable"] is False
    assert row["identity_evidence"] is False
    assert row["evidence_status"] == "metadata_only"
    assert report["resolution_summary"]["enriched_identity"] == 1
    units = json.loads(
        (
            mat_tmp / "out" / "task_local_increment"
            / "MATERIAL_UNITS_FINAL.json"
        ).read_text(encoding="utf-8")
    )["units"]
    prov = (
        units[0]["audit"]["source_provenance"]["dominant_review_unbundling"]
    )
    assert prov["reference_ordinal"] == "85"
    assert prov["useful_axes"] == ["axis-1", "axis-2"]
    assert prov["useful_sections"] == ["section-2"]
    assert prov["likely_evidence_roles"] == ["central_fact"]
    assert prov["acquisition_priority"] == "high"
    assert prov["true_abstract"] == "True abstract of the original."
    assert prov["open_access_url"] == "https://example.org/oa/enriched.pdf"
    assert prov["verified_identifiers"]["s2_paper_id"] == "S2:ENRICHED"
    assert prov["evidence_status"] == "metadata_only"
    assert prov["identity_evidence"] is False


def test_s2_service_outage_identity_fallback(mat_tmp: Path) -> None:
    requests = [{
        "reference_number": 14,
        "identity": {
            "doi": "10.1/down",
            "arxiv_id": "",
            "title": "Downstream original",
            "authors": "B. Author",
            "year": "2023",
            "s2_paper_id": "",
        },
        "acquisition_priority": "medium",
        "useful_axes": ["axis-3"],
        "useful_sections": ["section-3"],
        "likely_evidence_roles": ["method_or_measurement"],
    }]

    def batch_fn(ids):
        raise RuntimeError("S2 service unavailable")

    def search_fn(title):
        raise RuntimeError("S2 service unavailable")

    acquirer = _FakeOAAcquirer()
    report = _run(
        mat_tmp,
        requests,
        batch_fn=batch_fn,
        search_fn=search_fn,
        route_providers={
            "s2_structured_body": lambda request, resolved: [],
            "abstract_claim": lambda request, resolved: [],
        },
        oa_acquirer_factory=acquirer,
        oa_create_kb_fn=_fake_create_kb,
        oa_read_chunks_fn=(
            lambda kb_sqlite, paper_id: _oa_chunk_for(
                paper_id, title="Downstream original", doi="10.1/down"
            )
        ),
    )
    assert report["resolution_audit"]["s2_service_unavailable"] is True
    assert report["resolution_audit"]["batch_failed_call_count"] == 1
    row = report["per_request_audit"][0]
    assert row["resolution_mode"] == "identity_fallback"
    assert row["service_unavailable"] is True
    assert row["identity_evidence"] is False
    assert row["evidence_status"] == "metadata_only"
    assert report["resolution_summary"]["identity_fallback"] == 1
    assert report["resolution_summary"]["service_unavailable"] == 1
    units = json.loads(
        (
            mat_tmp / "out" / "task_local_increment"
            / "MATERIAL_UNITS_FINAL.json"
        ).read_text(encoding="utf-8")
    )["units"]
    prov = (
        units[0]["audit"]["source_provenance"]["dominant_review_unbundling"]
    )
    assert prov["evidence_status"] == "metadata_only"
    assert prov["identity_evidence"] is False
    assert prov["service_unavailable"] is True
    assert prov["reference_ordinal"] == "14"


def test_service_unavailable_distinguished_from_no_match(mat_tmp: Path) -> None:
    requests = [
        _request(1, "No match paper"),
        _request(2, "Outage paper"),
        _request(3, "Batch down", doi="10.1/down"),
        {
            "reference_number": 4,
            "identity": {
                "title": "No match fallback",
                "authors": "A. Author",
                "year": "2023",
            },
        },
    ]

    def batch_fn(ids):
        if "DOI:10.1/down" in ids:
            raise RuntimeError("S2 service unavailable")
        return [], None

    def search_fn(title):
        if title in {"No match paper", "No match fallback"}:
            return []
        raise RuntimeError("S2 service unavailable")

    results, audit = _resolve_requests_prefetch(
        requests,
        batch_fn=batch_fn,
        search_fn=search_fn,
    )
    assert results[0]["resolution_mode"] == "title_no_exact_match"
    assert results[0]["service_unavailable"] is False
    assert results[0]["resolved"] is None
    assert results[1]["resolution_mode"] == "s2_service_unavailable"
    assert results[1]["service_unavailable"] is True
    assert results[1]["resolved"] is None
    assert results[2]["resolution_mode"] == "identity_fallback"
    assert results[2]["service_unavailable"] is True
    assert results[2]["resolved"] is not None
    assert results[3]["resolution_mode"] == "no_s2_match_identity_fallback"
    assert results[3]["service_unavailable"] is False
    assert results[3]["resolved"] is not None
    assert audit["s2_service_unavailable"] is True
    assert audit["batch_failed_call_count"] == 1
    assert audit["identity_fallback_count"] == 1
    assert audit["no_s2_match_identity_fallback_count"] == 1
    assert audit["title_call_count"] == 4


def test_no_progress_does_not_publish_merged_snapshot(mat_tmp: Path) -> None:
    existing = [{
        "unit_id": "unit:text:existing",
        "work_id": "work:existing",
        "identity": {
            "chunk_id": "existing:0000",
            "paper_id": "S2:EXISTING",
            "doi": "10.1/existing",
            "title": "Existing paper",
        },
        "durable_content": {"raw_text": "existing"},
    }]
    base_units, base_vectors = _base_cache(mat_tmp, units=existing)
    finalize_calls: list[Any] = []
    merge_calls: list[Any] = []
    report = run_dominant_review_materialization(
        acquisition_requests_path=_write_requests(
            mat_tmp, [_request(1, "Existing paper", doi="10.1/existing")]
        ),
        base_units_path=base_units,
        base_vectors_path=base_vectors,
        output_dir=mat_tmp / "out",
        question="q",
        review_identity={"paper_id": "r", "title": "R"},
        extract_cards_fn=_fake_extract_cards,
        finalize_fn=lambda **kwargs: (
            finalize_calls.append(kwargs) or {"status": "finalized"}
        ),
        merge_fn=lambda **kwargs: (
            merge_calls.append(kwargs) or {"status": "merged"}
        ),
    )
    assert report["no_progress"] is True
    assert report["status"] == "no_progress"
    assert report["unit_count"] == 0
    assert report["merged_snapshot_path"] is None
    assert finalize_calls == []
    assert merge_calls == []
    assert not (mat_tmp / "out" / "merged_cache_snapshot").exists()
    assert (mat_tmp / "out" / "materialization_checkpoint.json").exists()


def test_resume_skips_completed_items(mat_tmp: Path) -> None:
    requests = [
        _request(1, "Done paper", s2="S2:DONE"),
        _request(2, "Pending paper", s2="S2:PENDING"),
    ]
    base_units, base_vectors = _base_cache(mat_tmp)
    output_dir = mat_tmp / "out"
    output_dir.mkdir()
    (output_dir / "task_local_increment").mkdir(parents=True)
    done_chunk = _s2_chunk(
        requests[0],
        _resolved(s2="S2:DONE", title="Done paper"),
    )
    checkpoint = {
        "schema_version": "dominant_review_materialization.v1",
        "output_dir": str(output_dir),
        "requests_total": 2,
        "kept_request_count": 2,
        "completed_items": [{
            "identity_key": _identity_key(requests[0]),
            "reference_number": "1",
            "status": "materialized",
            "route": "s2_structured_body",
            "resolution_mode": "exact_identity",
            "service_unavailable": False,
            "materialized_chunks": done_chunk,
            "unit_count": 1,
            "updated_at": "2026-01-01T00:00:00+00:00",
        }],
        "remaining_count": 1,
        "route_counts": {"s2_structured_body": 1},
        "per_request_audit": [{
            "reference_number": "1",
            "status": "materialized",
            "route": "s2_structured_body",
            "resolution_mode": "exact_identity",
            "service_unavailable": False,
        }],
        "unresolved_requests": [],
        "oa_audits": [],
        "dedupe_audit": [],
        "resolution_audit": {"mode": "injected_resolver"},
        "stages": {"requests_complete": False},
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    (output_dir / "materialization_checkpoint.json").write_text(
        json.dumps(checkpoint), encoding="utf-8"
    )
    resolver_calls: list[Any] = []

    def resolve_identity_fn(request):
        resolver_calls.append(request["reference_number"])
        return _resolved(
            s2="S2:PENDING", title="Pending paper"
        ), "exact_identity"

    report = run_dominant_review_materialization(
        acquisition_requests_path=_write_requests(mat_tmp, requests),
        base_units_path=base_units,
        base_vectors_path=base_vectors,
        output_dir=output_dir,
        question="q",
        review_identity={"paper_id": "r", "title": "R"},
        resume=True,
        extract_cards_fn=_fake_extract_cards,
        finalize_fn=_fake_finalize,
        merge_fn=_fake_merge,
        resolve_identity_fn=resolve_identity_fn,
        route_providers={
            "s2_structured_body": _s2_chunk,
            "abstract_claim": lambda request, resolved: [],
        },
    )
    assert resolver_calls == [2]
    assert report["unit_count"] == 2
    assert report["kept_request_count"] == 2
    assert [
        row["reference_number"] for row in report["per_request_audit"]
    ] == ["1", "2"]
    saved = json.loads(
        (output_dir / "materialization_checkpoint.json").read_text("utf-8")
    )
    assert len(saved["completed_items"]) == 2
    assert saved["remaining_count"] == 0


def test_expansion_request_preserves_enriched_metadata(mat_tmp: Path) -> None:
    from optomind_research.dominant_review_expansion import (
        build_acquisition_requests,
    )

    card = {
        "reference_number": 85,
        "identity": {
            "doi": "10.1/orig",
            "arxiv_id": "2606.21945",
            "title": "Old title",
            "authors": "A. Author",
            "year": "2024",
            "batch_lookup_ids": ["DOI:10.1/orig", "ARXIV:2606.21945"],
        },
        "enriched": {
            "title": "Enriched title",
            "abstract": "True abstract",
            "s2_paper_id": "S2:ENR",
            "openalex_id": "W1",
            "year": 2024,
            "authors": ["A. Author"],
            "venue": "Venue",
            "external_ids": {"DOI": "10.1/orig"},
            "open_access_url": "https://oa.example/x.pdf",
        },
        "screen": {
            "status": "kept",
            "decision": {
                "acquisition_priority": "high",
                "useful_axes": ["axis"],
                "useful_sections": ["section"],
                "likely_evidence_roles": ["central_fact"],
                "reason": "relevant",
                "relevance_score": 90,
                "keep": True,
            },
        },
    }
    payload = build_acquisition_requests([card])
    req = payload["requests"][0]
    assert req["reference_number"] == 85
    assert req["enriched"]["abstract"] == "True abstract"
    assert req["enriched"]["open_access_url"] == "https://oa.example/x.pdf"
    assert req["open_access_url"] == "https://oa.example/x.pdf"
    assert req["verified_identifiers"]["s2_paper_id"] == "S2:ENR"
    assert req["useful_axes"] == ["axis"]
    assert req["useful_sections"] == ["section"]
    assert req["likely_evidence_roles"] == ["central_fact"]


def _screening_batch_payload() -> dict[str, Any]:
    return {
        "contract_version": "v1",
        "batches": [{
            "batch_index": 1,
            "reference_numbers": [85],
            "status": "ok",
            "cards": [{
                "reference_number": 85,
                "candidate_text": "Synthetic Author 85. Enriched title.",
                "identity": {
                    "doi": "10.1/orig",
                    "arxiv_id": "2606.21945",
                    "title": "Enriched title",
                    "authors": "A. Author",
                    "year": "2024",
                    "s2_paper_id": "S2:ENR",
                    "openalex_id": "W1",
                    "batch_lookup_ids": ["DOI:10.1/orig", "ARXIV:2606.21945"],
                },
                "enriched": {
                    "title": "Enriched title",
                    "abstract": "True abstract",
                    "s2_paper_id": "S2:ENR",
                    "openalex_id": "W1",
                    "doi": "10.1/orig",
                    "arxiv_id": "2606.21945",
                    "year": 2024,
                    "authors": ["A. Author"],
                    "venue": "Venue",
                    "external_ids": {"DOI": "10.1/orig"},
                    "open_access_url": "https://oa.example/x.pdf",
                },
                "screen": {
                    "status": "kept",
                    "decision": {
                        "acquisition_priority": "high",
                        "useful_axes": ["axis"],
                        "useful_sections": ["section"],
                        "likely_evidence_roles": ["central_fact"],
                        "reason": "relevant",
                        "relevance_score": 90,
                        "keep": True,
                    },
                },
            }],
        }],
    }


def _old_request(reference_number: int = 85) -> dict[str, Any]:
    return {
        "reference_number": reference_number,
        "identity": {
            "doi": "10.1/orig",
            "arxiv_id": "",
            "title": "Old title",
            "authors": "",
            "year": "",
        },
        "acquisition_priority": "high",
        "review_secondary": {"paper_id": "review:paper"},
    }


def test_upgrade_requests_from_screening_batches_deterministic(
    mat_tmp: Path,
) -> None:
    from optomind_research.dominant_review_materialization import (
        upgrade_requests_from_screening_batches,
    )

    requests = [
        _old_request(85),
        {
            "reference_number": 7,
            "identity": {"title": "Unmatched"},
            "query_text": "",
        },
    ]
    upgraded, audit = upgrade_requests_from_screening_batches(
        requests, _screening_batch_payload()
    )
    assert audit["model_calls"] == 0
    assert audit["matched_card_count"] == 1
    assert audit["enriched_gained_count"] == 1
    assert audit["unmatched_reference_numbers"] == [7]
    req = upgraded[0]
    assert req["enriched"]["abstract"] == "True abstract"
    assert req["enriched"]["open_access_url"] == "https://oa.example/x.pdf"
    assert req["verified_identifiers"]["s2_paper_id"] == "S2:ENR"
    assert req["open_access_url"] == "https://oa.example/x.pdf"
    assert req["query_text"] == "Synthetic Author 85. Enriched title."
    assert req["useful_axes"] == ["axis"]
    assert req["useful_sections"] == ["section"]
    assert req["likely_evidence_roles"] == ["central_fact"]
    assert req["acquisition_priority"] == "high"
    assert req["review_secondary"] == {"paper_id": "review:paper"}
    assert upgraded[1] == requests[1]

    upgraded2, audit2 = upgrade_requests_from_screening_batches(
        upgraded, _screening_batch_payload()
    )
    assert audit2["enriched_already_present_count"] == 1
    assert audit2["enriched_gained_count"] == 0
    assert upgraded2[0] == upgraded[0]


def test_run_upgrades_old_requests_and_reuses_enriched_offline(
    mat_tmp: Path,
) -> None:
    batches_path = mat_tmp / "SCREENING_BATCHES.json"
    batches_path.write_text(
        json.dumps(_screening_batch_payload()), encoding="utf-8"
    )
    batch_calls: list[Any] = []
    search_calls: list[str] = []
    acquirer = _FakeOAAcquirer()
    report = _run(
        mat_tmp,
        [_old_request(85)],
        screening_batches_path=batches_path,
        batch_fn=lambda ids: (
            batch_calls.append(list(ids)) or ([], None)
        ),
        search_fn=lambda title: (
            search_calls.append(title) or []
        ),
        route_providers={
            "s2_structured_body": lambda request, resolved: [],
            "abstract_claim": lambda request, resolved: [],
        },
        oa_acquirer_factory=acquirer,
        oa_create_kb_fn=_fake_create_kb,
        oa_read_chunks_fn=(
            lambda kb_sqlite, paper_id: _oa_chunk_for(
                paper_id, title="Enriched title", doi="10.1/orig"
            )
        ),
    )
    assert batch_calls == []
    assert search_calls == []
    assert report["upgrade_audit"]["enriched_gained_count"] == 1
    assert report["question_seed_axes"]
    assert any(
        row["description"] == "axis"
        for row in report["question_seed_axes"]
    )
    assert all(
        row.get("origin") and row.get("status") == "seed"
        for row in report["question_seed_axes"]
    )
    row = report["per_request_audit"][0]
    assert row["resolution_mode"] == "enriched_identity"
    assert (
        mat_tmp / "out" / "ACQUISITION_REQUESTS_UPGRADED.json"
    ).exists()
    units = json.loads(
        (
            mat_tmp / "out" / "task_local_increment"
            / "MATERIAL_UNITS_FINAL.json"
        ).read_text(encoding="utf-8")
    )["units"]
    prov = (
        units[0]["audit"]["source_provenance"]["dominant_review_unbundling"]
    )
    assert prov["true_abstract"] == "True abstract"
    assert prov["open_access_url"] == "https://oa.example/x.pdf"


def test_resume_uses_upgraded_requests_artifact(mat_tmp: Path) -> None:
    batches_path = mat_tmp / "SCREENING_BATCHES.json"
    batches_path.write_text(
        json.dumps(_screening_batch_payload()), encoding="utf-8"
    )
    base_units, base_vectors = _base_cache(mat_tmp)
    requests_path = _write_requests(mat_tmp, [_old_request(85)])
    output_dir = mat_tmp / "out"
    report1 = run_dominant_review_materialization(
        acquisition_requests_path=requests_path,
        base_units_path=base_units,
        base_vectors_path=base_vectors,
        output_dir=output_dir,
        question="q",
        review_identity={"paper_id": "r", "title": "R"},
        screening_batches_path=batches_path,
        extract_cards_fn=_fake_extract_cards,
        finalize_fn=_fake_finalize,
        merge_fn=_fake_merge,
        batch_fn=lambda ids: ([], None),
        search_fn=lambda title: [],
        oa_acquirer_factory=_FakeOAAcquirer(),
        oa_create_kb_fn=_fake_create_kb,
        oa_read_chunks_fn=lambda kb_sqlite, paper_id: [],
        route_providers={
            "s2_structured_body": lambda request, resolved: [],
            "abstract_claim": _abstract_chunk,
        },
    )
    assert report1["unit_count"] == 1

    resolver_calls: list[Any] = []

    def resolve_identity_fn(request):
        resolver_calls.append(request)
        raise AssertionError("completed resume must not resolve requests")

    report2 = run_dominant_review_materialization(
        acquisition_requests_path=requests_path,
        base_units_path=base_units,
        base_vectors_path=base_vectors,
        output_dir=output_dir,
        question="q",
        review_identity={"paper_id": "r", "title": "R"},
        resume=True,
        extract_cards_fn=_fake_extract_cards,
        finalize_fn=_fake_finalize,
        merge_fn=_fake_merge,
        resolve_identity_fn=resolve_identity_fn,
        route_providers={
            "s2_structured_body": lambda request, resolved: [],
            "abstract_claim": lambda request, resolved: [],
        },
    )
    assert resolver_calls == []
    assert report2["unit_count"] == 1
    assert report2["upgrade_audit"]["enriched_gained_count"] == 1


def test_no_s2_match_metadata_fallback_oa_route(mat_tmp: Path) -> None:
    requests = [{
        "reference_number": 14,
        "identity": {
            "doi": "10.1/nomatch",
            "arxiv_id": "",
            "title": "No S2 match original",
            "authors": "B. Author",
            "year": "2023",
            "s2_paper_id": "",
        },
        "acquisition_priority": "medium",
    }]
    batch_calls: list[Any] = []
    search_calls: list[str] = []
    acquirer = _FakeOAAcquirer()
    report = _run(
        mat_tmp,
        requests,
        batch_fn=lambda ids: (
            batch_calls.append(list(ids)) or ([], None)
        ),
        search_fn=lambda title: (
            search_calls.append(title) or []
        ),
        route_providers={
            "s2_structured_body": lambda request, resolved: [],
            "abstract_claim": lambda request, resolved: [],
        },
        oa_acquirer_factory=acquirer,
        oa_create_kb_fn=_fake_create_kb,
        oa_read_chunks_fn=(
            lambda kb_sqlite, paper_id: _oa_chunk_for(
                paper_id, title="No S2 match original", doi="10.1/nomatch"
            )
        ),
    )
    assert batch_calls == [["DOI:10.1/nomatch"]]
    assert search_calls == ["No S2 match original"]
    row = report["per_request_audit"][0]
    assert row["resolution_mode"] == "no_s2_match_identity_fallback"
    assert row["service_unavailable"] is False
    assert row["identity_evidence"] is False
    assert report["route_counts"] == {"public_oa_fulltext": 1}
    assert report["resolution_summary"]["no_s2_match_identity_fallback"] == 1
    units = json.loads(
        (
            mat_tmp / "out" / "task_local_increment"
            / "MATERIAL_UNITS_FINAL.json"
        ).read_text(encoding="utf-8")
    )["units"]
    quality = units[0]["durable_content_card"]["content_quality"]
    assert quality["source_kind"] == "oa_fulltext"
    assert quality["evidence_ceiling"] == "factual_support"
    prov = (
        units[0]["audit"]["source_provenance"]["dominant_review_unbundling"]
    )
    assert prov["evidence_status"] == "metadata_only"
    assert prov["service_unavailable"] is False


def test_metadata_fallback_permission_is_discovery_only(mat_tmp: Path) -> None:
    from optomind_research.dominant_review_materialization import (
        _mapping_from_request_metadata,
        _paper_record_from_mapping,
        _record_to_mapping,
    )

    enriched_mapping = _mapping_from_request_metadata(
        _old_request(85), verified=True
    )
    fallback_mapping = _mapping_from_request_metadata(
        _old_request(14), verified=False
    )
    assert enriched_mapping["use_permission"] == "discovery_only"
    assert fallback_mapping["use_permission"] == "discovery_only"
    assert (
        _paper_record_from_mapping(fallback_mapping).use_permission
        == "discovery_only"
    )
    s2_record = _FakeS2Record("S2:ENR", "Enriched title", doi="10.1/orig")
    s2_mapping = _record_to_mapping(s2_record)
    assert s2_mapping["use_permission"] == "factual_support"


def test_question_seed_axes_merge_explicit_and_derived(mat_tmp: Path) -> None:
    from optomind_research.dominant_review_materialization import (
        _build_question_seed_axes,
    )

    explicit = [{
        "axis_id": "user:axis-1",
        "description": "Physics-informed training",
        "origin": "user_question",
        "status": "seed",
    }]
    requests = [
        {"useful_axes": ["Physics-informed training", "  PINN Generalization  "]},
        {
            "useful_axes": [
                "pinn generalization",
                "Convergence guarantees",
                "仿真可信度",
            ]
        },
    ]
    catalog = _build_question_seed_axes(explicit, requests)
    assert len(catalog) == 4
    labels = [
        row["description"].casefold().strip() for row in catalog
    ]
    assert labels == [
        "physics-informed training",
        "pinn generalization",
        "convergence guarantees",
        "仿真可信度",
    ]
    assert catalog[0]["axis_id"] == "user:axis-1"
    derived = [
        row for row in catalog
        if row["origin"] == "acquisition_request_useful_axes"
    ]
    assert len(derived) == 3
    assert all(row["status"] == "seed" for row in derived)
    assert all(row["axis_id"] for row in derived)
    again = _build_question_seed_axes(explicit, requests)
    assert [row["axis_id"] for row in again] == [
        row["axis_id"] for row in catalog
    ]


def test_packets_contain_global_axes_and_per_work_guidance(
    mat_tmp: Path,
) -> None:
    requests = [{
        "reference_number": 1,
        "identity": {
            "doi": "10.1/packet",
            "arxiv_id": "",
            "title": "Packet paper",
            "authors": "A. Author",
            "year": "2024",
            "s2_paper_id": "S2:PACKET",
        },
        "enriched": {
            "title": "Packet paper",
            "abstract": "Packet abstract.",
            "s2_paper_id": "S2:PACKET",
            "openalex_id": "W9",
            "doi": "10.1/packet",
            "open_access_url": "https://oa.example/packet.pdf",
        },
        "acquisition_priority": "high",
        "useful_axes": ["axis-1", "Explicit Axis"],
        "useful_sections": ["section-2"],
        "likely_evidence_roles": ["central_fact"],
        "reason": "kept",
    }]
    explicit_axes = [{
        "axis_id": "user:explicit-axis",
        "description": "Explicit Axis",
        "origin": "user_question",
        "status": "seed",
    }]
    report = _run(
        mat_tmp,
        requests,
        seed_axes=explicit_axes,
        route_providers={
            "s2_structured_body": lambda request, resolved: [],
            "abstract_claim": _abstract_chunk,
        },
        oa_acquirer_factory=_FakeOAAcquirer(),
        oa_create_kb_fn=_fake_create_kb,
        oa_read_chunks_fn=lambda kb_sqlite, paper_id: [],
    )
    assert report["question_seed_axes"]
    payload = json.loads(
        (
            mat_tmp / "out" / "task_local_increment"
            / "MATERIAL_CARD_PACKETS.json"
        ).read_text(encoding="utf-8")
    )
    packet = payload["packets"][0]
    catalog = packet["seed_axis_catalog"]
    labels = [row["description"] for row in catalog]
    assert "Explicit Axis" in labels
    assert "axis-1" in labels
    assert labels.count("Explicit Axis") == 1
    assert any(
        row["origin"] == "acquisition_request_useful_axes"
        for row in catalog
    )
    guidance = packet["supplementary_context"]["source_guidance"]
    assert guidance["useful_axes"] == ["Explicit Axis", "axis-1"]
    assert guidance["useful_sections"] == ["section-2"]
    assert guidance["likely_evidence_roles"] == ["central_fact"]
    assert guidance["reference_numbers"] == ["1"]
    assert guidance["acquisition_priority"] == "high"
    unb = packet["supplementary_context"]["dominant_review_unbundling"]
    assert unb["review_paper_id"] == "review:paper"


def test_cost_summary_in_report(mat_tmp: Path) -> None:
    requests = [_request(1, "Cost paper", s2="S2:COST")]
    resolved = _resolved(s2="S2:COST", title="Cost paper", abstract="Abstract.")
    report = _run(
        mat_tmp,
        requests,
        resolve_identity_fn=lambda request: (resolved, "exact_identity"),
        route_providers={
            "s2_structured_body": _s2_chunk,
            "abstract_claim": lambda request, resolved: [],
        },
    )
    cost = report["cost_summary"]
    assert cost["currency"] == "CNY"
    assert cost["qwen"]["model_call_count"] == 1
    assert cost["qwen"]["input_tokens"] == 10
    assert cost["qwen"]["output_tokens"] == 5
    assert cost["qwen"]["estimated_cost_cny"] > 0
    assert cost["qwen"]["per_model"]["b_plus_model"]["model_call_count"] == 1
    assert cost["embedding"]["input_tokens"] == 0
    assert cost["embedding"]["request_count"] == 0
    assert cost["embedding"]["estimated_cost_cny"] is None
    assert "not configured" in (cost["embedding"]["cost_omitted_reason"] or "")
    assert cost["priced_components"] == ["qwen_calls"]
    assert (
        cost["total_estimated_cost_cny"]
        == cost["qwen"]["estimated_cost_cny"]
    )
    assert "priced components" in cost["note"]


def test_incomplete_cards_block_merge_and_resume_retries_missing(
    mat_tmp: Path,
) -> None:
    requests = [
        _request(1, "Paper one", s2="S2:ONE"),
        _request(2, "Paper two", s2="S2:TWO"),
    ]
    resolved_map = {
        1: _resolved(s2="S2:ONE", title="Paper one"),
        2: _resolved(s2="S2:TWO", title="Paper two"),
    }
    resolver_calls: list[int] = []

    def resolve_identity_fn(request):
        number = int(request["reference_number"])
        resolver_calls.append(number)
        return resolved_map[number], "exact_identity"

    extractor = _ResumableCardExtractor(missing_indexes={1})
    merge_calls: list[Any] = []

    def merge_fn(**kwargs):
        merge_calls.append(kwargs)
        output_root = Path(kwargs["output_root"])
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "MERGE_REPORT.json").write_text(
            json.dumps({"status": "completed"}), encoding="utf-8"
        )
        return {"status": "completed", "output_root": str(output_root)}

    base_units, base_vectors = _base_cache(mat_tmp)
    common = dict(
        acquisition_requests_path=_write_requests(mat_tmp, requests),
        base_units_path=base_units,
        base_vectors_path=base_vectors,
        question="q",
        review_identity={"paper_id": "r", "title": "R"},
        extract_cards_fn=extractor,
        finalize_fn=_fake_finalize,
        merge_fn=merge_fn,
        resolve_identity_fn=resolve_identity_fn,
        route_providers={
            "s2_structured_body": _s2_chunk,
            "abstract_claim": lambda request, resolved: [],
        },
    )

    report1 = run_dominant_review_materialization(
        output_dir=mat_tmp / "out", **common
    )
    assert report1["status"] == "incomplete"
    assert report1["completed"] is False
    assert report1["expected_packet_count"] == 2
    assert report1["parsed_card_count"] == 1
    assert report1["merged_snapshot_path"] is None
    assert merge_calls == []
    assert not (mat_tmp / "out" / "merged_cache_snapshot").exists()
    assert resolver_calls == [1, 2]
    assert extractor.calls == 1
    assert report1["cost_summary"]["qwen"]["model_call_count"] == 1

    extractor.missing_indexes.clear()
    report2 = run_dominant_review_materialization(
        output_dir=mat_tmp / "out",
        resume=True,
        **common,
    )
    assert report2["status"] == "completed"
    assert report2["completed"] is True
    assert report2["expected_packet_count"] == 2
    assert report2["parsed_card_count"] == 2
    assert report2["card_count"] == 2
    assert (
        report2["merged_snapshot_path"]
        == str(mat_tmp / "out" / "merged_cache_snapshot")
    )
    assert len(merge_calls) == 1
    assert extractor.calls == 2
    assert resolver_calls == [1, 2]
    assert report2["cost_summary"]["qwen"]["model_call_count"] == 2
    assert report2["cost_summary"]["qwen"]["input_tokens"] == 20
    assert report2["cost_summary"]["qwen"]["output_tokens"] == 10


def _recording_extract_cards(observed: list[int]):
    def extractor(
        *,
        packet_path,
        output_dir,
        model_tier="b_plus_model",
        workers=1,
        skip_existing=True,
    ):
        observed.append(int(workers))
        return _fake_extract_cards(
            packet_path=packet_path,
            output_dir=output_dir,
            model_tier=model_tier,
            workers=workers,
            skip_existing=skip_existing,
        )

    return extractor


def _run_card_worker_case(
    mat_tmp: Path,
    requests: list[dict[str, Any]],
    *,
    output_name: str,
    extract_cards_fn,
    **overrides,
):
    base_units, base_vectors = _base_cache(mat_tmp)
    kwargs: dict[str, Any] = {
        "resolve_identity_fn": lambda request: (
            _resolved(
                s2=f"S2:{request['reference_number']}",
                title=request["identity"]["title"],
                doi=request["identity"].get("doi", ""),
                abstract="Abstract for offline card extraction.",
            ),
            "exact_identity",
        ),
        "route_providers": {
            "s2_structured_body": _s2_chunk,
            "abstract_claim": lambda request, resolved: [],
        },
    }
    kwargs.update(overrides)
    return run_dominant_review_materialization(
        acquisition_requests_path=_write_requests(mat_tmp, requests),
        base_units_path=base_units,
        base_vectors_path=base_vectors,
        output_dir=mat_tmp / output_name,
        question="Which originals support the review?",
        review_identity={"paper_id": "review:paper", "title": "Review"},
        extract_cards_fn=extract_cards_fn,
        finalize_fn=_fake_finalize,
        merge_fn=_fake_merge,
        **kwargs,
    )


def test_card_workers_default_is_bounded_three_and_audited(
    mat_tmp: Path, monkeypatch
) -> None:
    monkeypatch.delenv("OPTOMIND_CARD_EXTRACTION_WORKERS", raising=False)
    requests = [_request(1, "Paper one", doi="10.1/one")]
    observed: list[int] = []
    report = _run_card_worker_case(
        mat_tmp,
        requests,
        output_name="out",
        extract_cards_fn=_recording_extract_cards(observed),
    )

    assert observed == [3]
    assert report["status"] == "completed"
    assert report["card_worker_audit"] == {
        "workers": 3,
        "default": 3,
        "max": 8,
        "source": "default",
    }


def test_card_workers_clamped_and_serial_fallback(mat_tmp: Path) -> None:
    requests = [_request(1, "Paper one", doi="10.1/one")]
    cases: list[tuple[int | None, int]] = [
        (99, 8),
        (8, 8),
        (1, 1),
        (0, 1),
        (-1, 1),
    ]
    for index, (requested, expected) in enumerate(cases):
        observed: list[int] = []
        report = _run_card_worker_case(
            mat_tmp,
            requests,
            output_name=f"out_{index}",
            extract_cards_fn=_recording_extract_cards(observed),
            card_workers=requested,
        )
        assert observed == [expected]
        assert report["card_worker_audit"]["workers"] == expected
        assert report["card_worker_audit"]["source"] == "explicit"


def test_card_workers_env_override(mat_tmp: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPTOMIND_CARD_EXTRACTION_WORKERS", "2")
    requests = [_request(1, "Paper one", doi="10.1/one")]
    observed: list[int] = []
    report = _run_card_worker_case(
        mat_tmp,
        requests,
        output_name="out",
        extract_cards_fn=_recording_extract_cards(observed),
    )

    assert observed == [2]
    assert report["card_worker_audit"] == {
        "workers": 2,
        "default": 3,
        "max": 8,
        "source": "environment",
    }


def test_card_workers_preserve_checkpoint_and_deterministic_order(
    mat_tmp: Path,
) -> None:
    requests = [
        _request(1, "Paper one", doi="10.1/one"),
        _request(2, "Paper two", doi="10.2/two"),
    ]

    class RecordingResumableExtractor(_ResumableCardExtractor):
        def __init__(self, missing_indexes):
            super().__init__(missing_indexes)
            self.observed_workers: list[int] = []

        def __call__(
            self,
            *,
            packet_path,
            output_dir,
            model_tier="b_plus_model",
            workers=1,
            skip_existing=True,
        ):
            self.observed_workers.append(int(workers))
            return super().__call__(
                packet_path=packet_path,
                output_dir=output_dir,
                model_tier=model_tier,
                workers=workers,
                skip_existing=skip_existing,
            )

    extractor = RecordingResumableExtractor(missing_indexes={1})
    resolver_calls: list[int] = []

    def resolve_identity_fn(request):
        resolver_calls.append(request["reference_number"])
        if request["reference_number"] == 1:
            return _resolved(
                s2="S2:ONE",
                title="Paper one",
                doi="10.1/one",
                abstract="Abstract one.",
            ), "exact_identity"
        return _resolved(
            s2="S2:TWO",
            title="Paper two",
            doi="10.2/two",
            abstract="Abstract two.",
        ), "exact_identity"

    base_units, base_vectors = _base_cache(mat_tmp)
    common = dict(
        acquisition_requests_path=_write_requests(mat_tmp, requests),
        base_units_path=base_units,
        base_vectors_path=base_vectors,
        question="q",
        review_identity={"paper_id": "r", "title": "R"},
        extract_cards_fn=extractor,
        finalize_fn=_fake_finalize,
        merge_fn=_fake_merge,
        resolve_identity_fn=resolve_identity_fn,
        route_providers={
            "s2_structured_body": _s2_chunk,
            "abstract_claim": lambda request, resolved: [],
        },
        card_workers=3,
    )

    report1 = run_dominant_review_materialization(
        output_dir=mat_tmp / "out", **common
    )
    assert report1["status"] == "incomplete"
    assert report1["card_worker_audit"]["workers"] == 3

    extractor.missing_indexes.clear()
    report2 = run_dominant_review_materialization(
        output_dir=mat_tmp / "out", resume=True, **common
    )
    assert report2["status"] == "completed"
    assert report2["card_count"] == 2
    assert extractor.observed_workers == [3, 3]
    cards_root = (
        mat_tmp
        / "out"
        / "task_local_increment"
        / "material_cards"
        / "cards"
    )
    card_paths = sorted(path.name for path in cards_root.glob("*.json"))
    assert card_paths == sorted(set(card_paths))
    assert len(card_paths) == 2
