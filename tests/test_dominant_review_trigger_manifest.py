"""Focused tests for the blueprint candidate-claim-pool dominant-review trigger."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from optomind_research.dominant_review_expansion import (
    DOMINANT_REVIEW_TRIGGER_MANIFEST_VERSION,
    EXPANSION_QUOTA_CLASS,
    build_dominant_review_trigger_manifest,
)
from optomind_research.review_source_unpacking import (
    parse_numbered_bibliography,
)


@pytest.fixture()
def trigger_tmp() -> Path:
    """Workspace-local temp dir (pytest tmp_path is blocked in this sandbox)."""
    root = (
        Path(__file__).resolve().parent.parent
        / f"dominant-trigger-tmp-{uuid.uuid4().hex[:8]}"
    )
    os.makedirs(root, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


REVIEW_A_TITLE = (
    "Beyond Data-Driven: How Physics-Informed Neural Networks are "
    "Reshaping Multi-Physics Design and Discovery"
)
REVIEW_B_TITLE = (
    "Physics-Informed Neural Networks in Electromagnetic and "
    "Nanophotonic Design"
)


def _claim(claim_id: str, chunk_ids: list[str]) -> dict:
    return {
        "claim_id": claim_id,
        "claim_proposal_id": claim_id,
        "statement": f"Candidate claim {claim_id}.",
        "supporting_text_chunk_ids": chunk_ids,
    }


def _blueprint() -> dict:
    # Distribution over 95 candidate claims: A=60, B=20, D=12, C=3.
    distribution = [
        ("paperA", 60),
        ("paperB", 20),
        ("paperD", 12),
        ("paperC", 3),
    ]
    chunk_index = []
    claims: list[dict] = []
    claim_index = 0
    for paper_id, count in distribution:
        for offset in range(count):
            chunk_id = f"{paper_id}:chunk:{offset:03d}"
            chunk_index.append({
                "chunk_id": chunk_id,
                "paper_id": paper_id,
                "title": f"Chunk of {paper_id}",
            })
            claim_index += 1
            claim_id = f"S02-P{claim_index:03d}"
            # Every tenth claim reuses the previous chunk to exercise
            # duplicate chunk mapping without changing claim identity.
            chunk_ids = [chunk_id]
            if offset > 0 and offset % 10 == 0:
                chunk_ids.append(f"{paper_id}:chunk:{offset - 1:03d}")
            claims.append(_claim(claim_id, chunk_ids))
    return {
        "schema_version": "dynamic_review_blueprint.v4",
        "sections": [
            {
                "section_id": "S02",
                "title": "PINN multi-physics review section",
                "candidate_evidence_digest": {
                    "chunk_index": chunk_index,
                    "batches": [],
                },
                "candidate_claim_pool": {
                    "schema_version": "review_blueprint.candidate_claim_pool.v1",
                    "claims": claims,
                },
            }
        ],
    }


def _source_metadata() -> dict:
    return {
        "paperA": {
            "title": REVIEW_A_TITLE,
            "source_type": "review",
            "publication_types": ["review"],
            "body_text": "This review surveys the field and cites originals.",
            "raw_metadata": {"publicationTypes": ["review"]},
        },
        "paperB": {
            "title": REVIEW_B_TITLE,
            "source_type": "review",
            "publication_types": ["review"],
            "body_text": "Review of PINNs in electromagnetic design.",
            "raw_metadata": {"publicationTypes": ["review"]},
        },
        "paperC": {
            "title": "Below-threshold empirical study",
            "source_type": "original research",
        },
        "paperD": {
            "title": "Empirical trigger study on PINN solvers",
            "source_type": "original research",
            "publication_types": ["journalArticle"],
        },
    }


def _bibliography() -> dict:
    return {
        "paperA": {
            1: {"raw_text": '[1] A. Author, "PINN survey entry one," Journal, 2024.'},
            2: {"raw_text": '[2] B. Author, "PINN survey entry two," Journal, 2023.'},
        },
        "paperB": {
            1: {"raw_text": '[1] C. Author, "Electromagnetic PINN entry," Journal, 2024.'},
        },
    }


def _manifest(**kwargs):
    return build_dominant_review_trigger_manifest(
        _blueprint(),
        section_index=0,
        source_metadata=_source_metadata(),
        bibliography_by_source=_bibliography(),
        blueprint_path=Path("review_blueprint.json"),
        **kwargs,
    )


def test_two_review_triggers_and_below_threshold_and_non_review_skip() -> None:
    manifest = _manifest()
    assert manifest["schema_version"] == DOMINANT_REVIEW_TRIGGER_MANIFEST_VERSION
    assert manifest["section_id"] == "S02"
    assert manifest["denominator_count"] == 95
    assert manifest["audit"]["sources_considered"] == 4
    assert manifest["audit"]["triggered_sources"] == 3
    assert manifest["audit"]["review_confirmed_unpack_tasks"] == 2

    tasks_by_source = {
        task["source_id"]: task for task in manifest[
            "review_confirmed_unpack_tasks"
        ]
    }
    assert set(tasks_by_source) == {"paperA", "paperB"}
    task_a = tasks_by_source["paperA"]
    task_b = tasks_by_source["paperB"]
    assert task_a["claim_count"] == 60
    assert task_b["claim_count"] == 20
    assert task_a["claim_share"] == round(60 / 95, 4)
    assert task_b["claim_share"] == round(20 / 95, 4)
    assert len(task_a["chunk_ids"]) == 60
    assert len(task_b["chunk_ids"]) == 20
    assert task_a["source_identity"]["title"] == REVIEW_A_TITLE
    assert task_b["source_identity"]["title"] == REVIEW_B_TITLE
    assert task_a["source_type"] == "review_unbundling"
    assert task_a["review_confirmation"]["confirmed"] is True
    assert len(task_a["reference_bibliography"]) == 2
    assert len(task_b["reference_bibliography"]) == 1
    assert task_a["deduplication_audit"]["kept_record_count"] == 2
    assert task_b["deduplication_audit"]["kept_record_count"] == 1
    assert len(task_a["s2_enrichment_plan"]) == 2
    assert len(task_a["material_cache_contract"]) == 2
    assert all(
        plan["exact_match"]["no_title_guess_when_ids_unavailable"] is True
        for plan in task_a["s2_enrichment_plan"]
    )
    assert all(
        plan["provenance"]["original_primary_on_conflict"] is True
        for plan in task_a["material_cache_contract"]
    )

    skipped_reasons = manifest["audit"]["skipped_reasons"]
    assert skipped_reasons == {"below_threshold": 1, "not_review_source": 1}
    assert "paperD" not in tasks_by_source
    assert "paperC" not in tasks_by_source


def test_non_quota_policy_and_no_fabricated_ids() -> None:
    manifest = _manifest()
    policy = manifest["non_quota_policy"]
    assert policy["quota_class"] == EXPANSION_QUOTA_CLASS
    assert policy["non_quota"] is True
    assert policy["no_count_cap"] is True
    assert policy["admission_cap"] is None
    assert policy["ordinary_quota_decrement"] == 0
    assert policy["s2_oa_section_supplementary_quota_consumed"] is False
    assert manifest["evidence_precedence"]["evidence_precedence"] == (
        "original_primary"
    )
    # Ordinary retrieval/admission configs must not leak into this channel.
    assert "served_text_limit" not in manifest
    assert "retrieval_max_total" not in manifest
    assert "admission_threshold" not in manifest
    # No fabricated reference ids: only provided bibliography is emitted.
    for task in manifest["review_confirmed_unpack_tasks"]:
        for reference in task["references"]:
            identity = reference["identity"]
            assert identity["doi"] == ""
            assert identity["batch_lookup_ids"] == []
            assert identity["lookup_ids_empty"] is True
            assert reference["raw_text"]


def test_manifest_preserves_source_identities_and_plans() -> None:
    manifest = _manifest()
    task = manifest["review_confirmed_unpack_tasks"][0]
    assert task["reference_list_acquisition_plan"]["non_quota"] is True
    assert (
        task["reference_list_acquisition_plan"]["bibliography_parser"]
        == "parse_numbered_bibliography(mode='whole_document')"
    )
    assert task["acquisition_contract"]["no_top_n_quota"] is True
    assert task["quota_class"] == EXPANSION_QUOTA_CLASS
    assert task["non_quota"] is True
    assert task["ordinary_quota_decrement"] == 0
    assert task["source_policy"]["role"] == (
        "review_secondary_for_synthesis_history"
    )
    # Claim ids and chunk ids from the blueprint are preserved verbatim.
    task_a = next(
        row for row in manifest["review_confirmed_unpack_tasks"]
        if row["source_id"] == "paperA"
    )
    assert task_a["claim_ids"][:2] == ["S02-P001", "S02-P002"]
    assert "paperA:chunk:000" in task_a["chunk_ids"]


def test_direct_script_help_import_path_works_from_repo_root() -> None:
    project_root = Path(__file__).resolve().parents[1]
    for script, expected in (
        ("scripts/run_dominant_review_trigger_manifest.py", b"--blueprint"),
        ("scripts/run_dominant_review_expansion_real.py", b"--probe-json"),
    ):
        result = subprocess.run(
            [sys.executable, script, "--help"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert expected in result.stdout.encode("utf-8")


def test_runner_end_to_end_persists_manifest(trigger_tmp: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    blueprint_path = trigger_tmp / "blueprint.json"
    blueprint_path.write_text(
        json.dumps(_blueprint()), encoding="utf-8"
    )
    metadata_path = trigger_tmp / "metadata.json"
    metadata_path.write_text(
        json.dumps(_source_metadata()), encoding="utf-8"
    )
    bibliography_path = trigger_tmp / "bibliography.json"
    bibliography_path.write_text(
        json.dumps(_bibliography()), encoding="utf-8"
    )
    output_path = trigger_tmp / "manifest.json"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/run_dominant_review_trigger_manifest.py",
            "--blueprint",
            str(blueprint_path),
            "--section-index",
            "0",
            "--source-metadata",
            str(metadata_path),
            "--bibliography",
            str(bibliography_path),
            "--output",
            str(output_path),
        ],
        cwd=str(project_root),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output_path.exists()
    manifest = json.loads(output_path.read_text(encoding="utf-8"))
    assert manifest["audit"]["review_confirmed_unpack_tasks"] == 2
    assert manifest["blueprint_path"] == str(blueprint_path)


def _single_paper_blueprint(paper_id: str = "paperA") -> dict:
    return {
        "sections": [
            {
                "section_id": "S02",
                "candidate_evidence_digest": {
                    "chunk_index": [
                        {
                            "chunk_id": f"chunk:{paper_id}:000",
                            "paper_id": paper_id,
                            "title": paper_id,
                        },
                        {
                            "chunk_id": f"chunk:{paper_id}:001",
                            "paper_id": paper_id,
                            "title": paper_id,
                        },
                    ],
                    "batches": [],
                },
                "candidate_claim_pool": {
                    "schema_version": "review_blueprint.candidate_claim_pool.v1",
                    "claims": [
                        {
                            "claim_id": "S02-P001",
                            "claim_proposal_id": "S02-P001",
                            "statement": "Candidate claim one.",
                            "supporting_text_chunk_ids": [
                                f"chunk:{paper_id}:000"
                            ],
                        },
                        {
                            "claim_id": "S02-P002",
                            "claim_proposal_id": "S02-P002",
                            "statement": "Candidate claim two.",
                            "supporting_text_chunk_ids": [
                                f"chunk:{paper_id}:001"
                            ],
                        },
                    ],
                },
            }
        ]
    }


def test_flattened_single_line_bibliography_parses_after_heading() -> None:
    flattened = (
        '71 References [1] A. Author, "Title One," Journal, 2024. '
        '[2] B. Author, "Title Two," Journal, 2023. '
        '[3] C. Author, "Title Three," Journal, 2022.'
    )
    parsed = parse_numbered_bibliography(
        flattened, mode="whole_document"
    )
    assert sorted(parsed.keys()) == [1, 2, 3]
    assert parsed.audit["flattened_single_line"] is True
    assert parsed.audit["heading_found"] is True
    assert parsed.audit["entry_count"] == 3
    assert parsed.audit["flattened_entry_count"] == 3
    assert parsed.audit["flattened_sequence_checks"][
        "starts_at_1"
    ] is True
    assert parsed[1]["candidate_text"].startswith("A. Author")
    assert parsed[3]["candidate_text"].startswith("C. Author")


def test_flattened_bibliography_may_continue_across_cached_chunks() -> None:
    flattened = (
        '71 References [1] A. Author, "Title One," Journal, 2024.\n'
        '[2] B. Author, "Title Two," Journal, 2023. '
        '[3] C. Author, "Title Three," Journal, 2022.'
    )
    parsed = parse_numbered_bibliography(
        flattened, mode="whole_document"
    )
    assert sorted(parsed.keys()) == [1, 2, 3]
    assert parsed.audit["flattened_single_line"] is True
    assert parsed[1]["entry_start"] == flattened.index("[1]")
    assert parsed[3]["candidate_text"].startswith("C. Author")


def test_inline_body_citations_not_treated_as_bibliography() -> None:
    for text in (
        "The method is described in References [1], [2], and [3] for details.",
        "References [1], [2], and [3] are discussed below.",
        "See [1], [2], [3] for related work.",
    ):
        parsed = parse_numbered_bibliography(
            text, mode="whole_document"
        )
        assert parsed == {}
        assert parsed.audit["flattened_single_line"] is False


def test_caller_confirmed_review_with_missing_bibliography_skips_truthfully(
    trigger_tmp: Path,
) -> None:
    manifest = build_dominant_review_trigger_manifest(
        _single_paper_blueprint("paperA"),
        section_index=0,
        source_metadata={
            "paperA": {
                "title": "Approved Multi-Physics Design Source",
                "source_type": "unknown",
            }
        },
        bibliography_by_source={},
        confirmed_review_paper_ids=["paperA"],
        blueprint_path=Path("blueprint.json"),
    )
    assert manifest["audit"]["review_confirmed_unpack_tasks"] == 0
    paper_row = next(
        row for row in manifest["source_rows"]
        if row["source_id"] == "paperA"
    )
    assert paper_row["triggered"] is True
    skip = next(
        row for row in manifest["skipped_sources"]
        if row["source_id"] == "paperA"
    )
    assert skip["skip_reason"] == "missing_bibliography"
    assert skip["skip_reason"] != "not_review_source"
    assert manifest["audit"]["skipped_reasons"] == {
        "missing_bibliography": 1
    }


def test_flattened_bibliography_feeds_unpack_task(trigger_tmp: Path) -> None:
    flattened = (
        'References [1] A. Author, "Title One," Journal, 2024. '
        '[2] B. Author, "Title Two," Journal, 2023. '
        '[3] C. Author, "Title Three," Journal, 2022.'
    )
    bibliography = parse_numbered_bibliography(
        flattened, mode="whole_document"
    )
    manifest = build_dominant_review_trigger_manifest(
        _single_paper_blueprint("paperA"),
        section_index=0,
        source_metadata={
            "paperA": {
                # Real first S02 review: no review word in the title; the
                # source is caller-confirmed rather than type-classified.
                "title": (
                    "Beyond Data-Driven: How Physics-Informed Neural "
                    "Networks are Reshaping Multi-Physics Design and "
                    "Discovery"
                ),
                "source_type": "unknown",
                "body_text": flattened,
            }
        },
        bibliography_by_source={"paperA": bibliography},
        confirmed_review_paper_ids=["paperA"],
        blueprint_path=Path("blueprint.json"),
    )
    task = manifest["review_confirmed_unpack_tasks"][0]
    assert task["source_id"] == "paperA"
    assert task["source_type"] == "review_unbundling_caller_confirmed"
    assert task["review_confirmation"]["source"] == "caller_confirmed"
    assert task["review_confirmation"]["confirmed"] is True
    assert len(task["reference_bibliography"]) == 3
    assert len(task["references"]) == 3
    assert len(task["s2_enrichment_plan"]) == 3
    assert len(task["material_cache_contract"]) == 3
    assert task["non_quota"] is True
    assert task["no_admission_cap"] is True
