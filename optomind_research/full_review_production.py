"""Writing, audit, review, and delivery adapters for full-review S13-S20.

The functions in this module preserve three boundaries:

1. A retrieved source is not automatically a citation.
2. A readable draft is not automatically a formally ready review.
3. Supervisor and peer-review agents propose changes; only explicitly accepted
   suggestions may modify prose.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from llm.qwen_chat_client import call_qwen_chat

from optomind_research.full_review_evidence import compact, resolve_kb_sqlite
from optomind_research.review_writer import (
    CitationBinder,
    EvidenceAwareRevisionAgent,
    EvidencePacket,
    FinalTranslator,
    OverclaimAuditor,
    ReviewWritingPipeline,
    SectionDraft,
    SectionMaterialPacket,
    audit_manuscript_continuity,
    remove_broken_visual_promises,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = PROJECT_ROOT / "prompts"
SECTION_QUALITY_PROMPT = PROMPTS_DIR / "Review Writing Quality Judge.txt"
GLOBAL_REVIEW_PROMPT = PROMPTS_DIR / "Global Review Judge.txt"
PEER_REVIEW_PROMPT = PROMPTS_DIR / "Peer Review Panel.txt"
FINAL_HEADING_TRANSLATOR_PROMPT = PROMPTS_DIR / "Final Chinese Heading Translator.txt"
M4_CONTRACT_SCHEMA_VERSION = "full_review.m4_contract.v1"
M4_PROPOSAL_SCHEMA_VERSION = (
    "optomind.global_manuscript_commander.m4_proposal.v1"
)


def _safe_json(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(text or ""))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", str(text or ""), re.S)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}


def _normalize_five_point_scores(result: dict[str, Any]) -> dict[str, Any]:
    """Normalize critic scores to the declared 0-5 protocol.

    Even with an explicit schema, a model can occasionally return an overall
    percentage while its dimension scores use 0-5.  Preserve the raw anomaly
    in an audit field and convert it deterministically instead of letting an
    incomparable value propagate into release decisions.
    """
    if not isinstance(result, dict):
        return result
    changes: dict[str, Any] = {}

    def normalize(value: Any, label: str) -> float:
        try:
            raw = float(value)
        except (TypeError, ValueError):
            changes[label] = {"raw": value, "normalized": 0.0, "reason": "non_numeric"}
            return 0.0
        normalized = raw / 20.0 if 5.0 < raw <= 100.0 else raw
        normalized = max(0.0, min(5.0, normalized))
        normalized = round(normalized, 2)
        if normalized != raw:
            changes[label] = {
                "raw": raw,
                "normalized": normalized,
                "reason": "converted_percent_or_clamped_to_0_5",
            }
        return normalized

    scores = result.get("scores")
    if isinstance(scores, dict):
        result["scores"] = {
            str(name): normalize(value, f"scores.{name}")
            for name, value in scores.items()
        }
    result["overall_score"] = normalize(
        result.get("overall_score", 0), "overall_score"
    )
    if changes:
        result["score_scale_audit"] = changes
    return result


def draft_to_dict(draft: SectionDraft) -> dict[str, Any]:
    return {
        "section_id": draft.section_id,
        "english_text": draft.english_text,
        "chinese_text": draft.chinese_text,
        "citation_map": draft.citation_map,
        "overclaim_flags": draft.overclaim_flags,
        "contradiction_notes": draft.contradiction_notes,
        "figure_placements": draft.figure_placements,
        "status": draft.status,
        "uncited_load_bearing": draft.uncited_load_bearing,
        "revision_history": draft.revision_history,
    }


def draft_from_dict(row: dict[str, Any]) -> SectionDraft:
    return SectionDraft(
        section_id=str(row.get("section_id") or ""),
        english_text=str(row.get("english_text") or ""),
        chinese_text=str(row.get("chinese_text") or ""),
        citation_map=dict(row.get("citation_map") or {}),
        overclaim_flags=list(row.get("overclaim_flags") or []),
        contradiction_notes=list(row.get("contradiction_notes") or []),
        figure_placements=list(row.get("figure_placements") or []),
        status=str(row.get("status") or "draft"),
        uncited_load_bearing=list(row.get("uncited_load_bearing") or []),
        revision_history=list(row.get("revision_history") or []),
    )


def packet_from_dict(row: dict[str, Any]) -> SectionMaterialPacket:
    packets = [
        EvidencePacket(
            claim_id=str(item.get("claim_id") or ""),
            paper_id=str(item.get("paper_id") or ""),
            chunk_id=str(item.get("chunk_id") or ""),
            exact_spans=list(item.get("exact_spans") or []),
            visual_refs=list(item.get("visual_refs") or []),
            support_relation=str(item.get("support_relation") or "component_support"),
            limitations=list(item.get("limitations") or []),
            evidence_level=str(item.get("evidence_level") or "fulltext"),
            source_kind=str(item.get("source_kind") or "fulltext"),
            scope_fit=str(item.get("scope_fit") or "in_domain"),
            retrieval_role=str(item.get("retrieval_role") or "evidence_candidate"),
            source_title=str(item.get("source_title") or ""),
        )
        for item in (row.get("evidence_packets") or [])
        if isinstance(item, dict)
    ]
    return SectionMaterialPacket(
        section_id=str(row.get("section_id") or ""),
        section_contract=dict(row.get("section_contract") or {}),
        claims=list(row.get("claims") or []),
        evidence_packets=packets,
        contradictions=list(row.get("contradictions") or []),
        open_questions=list(row.get("open_questions") or []),
        transition_contract=dict(row.get("transition_contract") or {}),
        uncited_load_bearing_claim_ids=list(row.get("uncited_load_bearing_claim_ids") or []),
        visual_evidence=list(row.get("visual_evidence") or []),
        visual_gap_plan=list(row.get("visual_gap_plan") or []),
        manuscript_context=dict(row.get("manuscript_context") or {}),
        literature_coverage=dict(row.get("literature_coverage") or {}),
    )


def _review_text(drafts: list[SectionDraft], blueprint: dict[str, Any]) -> str:
    title_by_id = {
        str(section.get("section_id") or ""): compact(
            section.get("title") or section.get("section_title"), 240
        )
        for section in (blueprint.get("sections") or [])
    }
    blocks: list[str] = []
    for draft in drafts:
        title = title_by_id.get(draft.section_id) or draft.section_id
        blocks.append(f"## {title}\n\n{draft.english_text.strip()}")
    return "\n\n".join(blocks).strip() + "\n"


def _word_budget_compliance(
    draft: SectionDraft,
    *,
    word_budget: int,
    actual_word_count: int,
) -> tuple[bool, str, float]:
    """Evaluate length without turning a safety edit into a two-word failure.

    The writer must first reach the normal 80% threshold.  If a later citation
    or overclaim safety pass removes a small amount of prose, a transparent 2%
    drift allowance is permitted.  This is not a general lowering of the
    writing target: drafts that never reached 80%, exceed the upper bound, or
    lack an accepted repair remain non-compliant.
    """
    if not word_budget:
        return True, "not_applicable", 0.0
    ratio = actual_word_count / word_budget
    if 0.80 <= ratio <= 1.15:
        return True, "strict", ratio
    reached_minimum_before_safety_edit = any(
        isinstance(row, dict)
        and str(row.get("stage") or "").startswith("section_contract_repair")
        and bool(row.get("accepted"))
        and bool(row.get("meets_80_percent_budget"))
        for row in draft.revision_history
    )
    if reached_minimum_before_safety_edit and 0.78 <= ratio < 0.80:
        return True, "post_audit_safety_drift_tolerance", ratio
    return False, "failed", ratio


def write_section_drafts(
    visual_bundle: dict[str, Any],
    *,
    kb_path: Path | str | None,
    real_llm: bool,
    checkpoint_dir: Path,
) -> dict[str, Any]:
    """Implement S13 without performing the later S15 global edit."""
    blueprint = copy.deepcopy(visual_bundle.get("blueprint") or {})
    sqlite_path = resolve_kb_sqlite(kb_path or visual_bundle.get("kb_sqlite"))
    pipeline = ReviewWritingPipeline(
        real_llm=real_llm,
        kb_path=sqlite_path,
        checkpoint_dir=checkpoint_dir,
        resume=True,
    )
    drafts = pipeline.run(blueprint, translate=False, cross_section_edit=False)
    packet_rows = [packet.to_dict() for packet in pipeline.last_packets]
    failed = [draft.section_id for draft in drafts if draft.status in {"failed", "audit_failed"}]
    empty = [draft.section_id for draft in drafts if not draft.english_text.strip()]
    review = _review_text(drafts, blueprint)
    packet_by_id = {packet.section_id: packet for packet in pipeline.last_packets}
    contract_compliance: list[dict[str, Any]] = []
    for draft in drafts:
        contract = packet_by_id.get(draft.section_id, SectionMaterialPacket(draft.section_id)).section_contract
        try:
            budget = max(0, int(contract.get("word_budget") or 0))
        except (TypeError, ValueError):
            budget = 0
        actual_words = len(re.findall(r"\b[A-Za-z][A-Za-z'-]*\b", draft.english_text))
        expected_paragraphs = len(contract.get("paragraph_functions") or [])
        actual_paragraphs = len([
            row for row in re.split(r"\n\s*\n", draft.english_text) if row.strip()
        ])
        word_budget_compliant, compliance_mode, ratio = _word_budget_compliance(
            draft,
            word_budget=budget,
            actual_word_count=actual_words,
        )
        contract_compliance.append({
            "section_id": draft.section_id,
            "word_budget": budget,
            "actual_word_count": actual_words,
            "word_budget_ratio": round(ratio, 3) if budget else None,
            "word_budget_compliant": word_budget_compliant,
            "word_budget_compliance_mode": compliance_mode,
            "expected_paragraph_count": expected_paragraphs,
            "actual_paragraph_count": actual_paragraphs,
            "paragraph_contract_compliant": (
                not expected_paragraphs
                or actual_paragraphs >= (
                    1 if expected_paragraphs == 1 else max(2, expected_paragraphs - 1)
                )
            ),
        })
    return {
        "schema_version": "full_review.section_drafts.v1",
        "blueprint": blueprint,
        "section_drafts": [draft_to_dict(draft) for draft in drafts],
        "material_packets": packet_rows,
        "full_review_english": review,
        "kb_sqlite": str(sqlite_path or ""),
        "quality_summary": {
            "section_count": len(drafts),
            "failed_section_ids": failed,
            "empty_section_ids": empty,
            "uncited_load_bearing_claim_count": sum(
                len(draft.uncited_load_bearing) for draft in drafts
            ),
            "accepted_citation_count": sum(
                len(chunk_ids)
                for draft in drafts
                for chunk_ids in draft.citation_map.values()
            ),
            "overclaim_flag_count": sum(len(draft.overclaim_flags) for draft in drafts),
            "english_word_count": len(re.findall(r"\b[A-Za-z][A-Za-z'-]*\b", review)),
            "contract_compliance": contract_compliance,
            "sections_below_word_budget": [
                row["section_id"] for row in contract_compliance
                if row["word_budget"] and not row["word_budget_compliant"]
            ],
            "sections_with_paragraph_contract_failure": [
                row["section_id"] for row in contract_compliance
                if not row["paragraph_contract_compliant"]
            ],
            "draft_ready_for_audit": bool(
                not failed
                and not empty
                and not [
                    row for row in contract_compliance
                    if row["word_budget"] and not row["word_budget_compliant"]
                ]
                and not [
                    row for row in contract_compliance
                    if not row["paragraph_contract_compliant"]
                ]
            ),
        },
    }


def _compact_packet_for_judge(packet: SectionMaterialPacket) -> dict[str, Any]:
    return {
        "section_id": packet.section_id,
        "section_contract": packet.section_contract,
        "claims": [
            {
                "claim_id": claim.get("claim_id"),
                "authorized_statement": claim.get("statement_for_writing") or claim.get("statement"),
                "writing_permission": claim.get("writing_permission"),
                "evidence_binding_status": claim.get("evidence_binding_status"),
                "excluded_missing_components": list(
                    claim.get("missing_evidence_components") or []
                ),
            }
            for claim in packet.claims
            if isinstance(claim, dict)
        ],
        "evidence_packets": [
            {
                **ep.to_dict(),
                "exact_spans": [compact(span, 500) for span in ep.exact_spans[:2]],
            }
            for ep in packet.evidence_packets[:24]
        ],
        "open_questions": packet.open_questions,
        "uncited_load_bearing_claim_ids": packet.uncited_load_bearing_claim_ids,
        "visual_evidence": packet.visual_evidence[:12],
        "visual_gap_plan": packet.visual_gap_plan[:8],
        "literature_coverage": {
            "plan": packet.literature_coverage.get("plan") or {},
            "sources": [
                {
                    "paper_id": row.get("paper_id"),
                    "title": row.get("title"),
                    "year": row.get("year"),
                    "coverage_roles": list(row.get("coverage_roles") or []),
                    "intended_uses": [
                        use.get("intended_synthesis")
                        for use in (row.get("role_uses") or [])[:3]
                        if isinstance(use, dict)
                    ],
                }
                for row in (packet.literature_coverage.get("sources") or [])[:30]
                if isinstance(row, dict)
            ],
            "coverage_gaps": list(packet.literature_coverage.get("coverage_gaps") or [])[:12],
        },
    }


def _judge_section_quality(
    draft: SectionDraft,
    packet: SectionMaterialPacket,
    *,
    real_llm: bool,
) -> dict[str, Any]:
    if not real_llm:
        return {
            "scores": {},
            "overall_score": 0,
            "verdict": "mock_not_evaluated",
            "strengths": [],
            "material_failures": [],
            "highest_value_revision": "Run the real quality judge before formal use.",
            "cross_domain_misuse_detected": False,
            "unsupported_fact_detected": False,
        }
    payload = {
        "material_packet": _compact_packet_for_judge(packet),
        "draft": draft_to_dict(draft),
    }
    messages = [
        {"role": "system", "content": SECTION_QUALITY_PROMPT.read_text(encoding="utf-8")},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    # A quality-judge transport/JSON failure is an infrastructure problem, not
    # evidence that the manuscript itself is unsupported.  Use a short,
    # explicit escalation ladder so the stage can self-heal without triggering
    # an uncontrolled model/key fallback explosion.
    attempts: list[dict[str, Any]] = []
    for attempt_index, model_tier in enumerate(
        ("premium_model", "b_plus_model", "b_minus_model"), 1
    ):
        result = call_qwen_chat(
            f"ReviewWritingQualityJudge:attempt_{attempt_index}",
            messages,
            model_tier=model_tier,
            temperature=0,
            max_tokens=3200,
            response_format={"type": "json_object"},
            force_mock=False,
            max_retries=0,
            timeout_seconds=150,
            max_transport_key_candidates=1,
            allow_model_fallback=False,
        )
        usage = result.get("_llm_usage") if isinstance(result, dict) else {}
        parsed = _safe_json(str((result or {}).get("content") or ""))
        attempts.append({
            "attempt": attempt_index,
            "model_tier": model_tier,
            "transport_success": bool((usage or {}).get("success", True)),
            "valid_json": bool(parsed),
            "error_type": str((usage or {}).get("error_type") or ""),
        })
        if parsed:
            normalized = _normalize_five_point_scores(parsed)
            normalized["judge_process"] = {
                "attempt_count": attempt_index,
                "selected_model_tier": model_tier,
                "attempts": attempts,
            }
            return normalized
    return {
        "scores": {},
        "overall_score": 0,
        "verdict": "judge_failed",
        "material_failures": [
            "The independent section quality judge was unavailable after bounded retries."
        ],
        "unsupported_fact_detected": False,
        "infrastructure_failure": True,
        "judge_process": {
            "attempt_count": len(attempts),
            "selected_model_tier": "",
            "attempts": attempts,
        },
    }


def audit_citations(
    draft_bundle: dict[str, Any],
    *,
    real_llm: bool,
) -> dict[str, Any]:
    """Implement S14: structural citation audit plus independent quality judgment."""
    drafts = [
        remove_broken_visual_promises(draft_from_dict(row))
        for row in (draft_bundle.get("section_drafts") or [])
    ]
    packets = [packet_from_dict(row) for row in (draft_bundle.get("material_packets") or [])]
    packet_by_id = {packet.section_id: packet for packet in packets}

    def audit_one(draft: SectionDraft) -> dict[str, Any]:
        packet = packet_by_id.get(draft.section_id, SectionMaterialPacket(draft.section_id))
        coverage_chunk_ids = {
            str(chunk.get("chunk_id") or "")
            for source in (packet.literature_coverage.get("sources") or [])
            if isinstance(source, dict)
            for chunk in (source.get("representative_chunks") or [])
            if isinstance(chunk, dict) and chunk.get("chunk_id")
        }
        valid_chunk_ids = (
            {ep.chunk_id for ep in packet.evidence_packets if ep.chunk_id}
            | coverage_chunk_ids
        )
        cited_chunk_ids = {
            str(chunk_id)
            for values in draft.citation_map.values()
            for chunk_id in values
            if chunk_id
        }
        invalid = sorted(cited_chunk_ids - valid_chunk_ids)
        claim_to_chunks: dict[str, set[str]] = {}
        for ep in packet.evidence_packets:
            claim_to_chunks.setdefault(ep.claim_id, set()).add(ep.chunk_id)
        load_bearing_factual = {
            str(claim.get("claim_id") or "")
            for claim in packet.claims
            if bool(claim.get("load_bearing"))
            and str(claim.get("evidence_requirement") or "factual") == "factual"
        }
        uncited = sorted(
            claim_id
            for claim_id in load_bearing_factual
            if not (claim_to_chunks.get(claim_id, set()) & cited_chunk_ids)
        )
        unresolved_markers = re.findall(r"\[REF:([^\]]+)\]", draft.english_text)
        rejected_entailment_sentences = []
        for row in draft.overclaim_flags:
            if (
                not isinstance(row, dict)
                or row.get("overclaim_type") != "uncited_after_entailment_rejection"
                or bool(row.get("resolved"))
            ):
                continue
            fragment = str(row.get("sentence_fragment") or "").strip()
            revised = str(row.get("revised_sentence") or "").strip()
            # Historical audit records remain useful provenance, but an issue
            # no longer blocks the current manuscript after the offending
            # sentence has been removed or replaced.
            if fragment and fragment not in draft.english_text:
                if not revised or revised not in draft.english_text:
                    continue
            rejected_entailment_sentences.append(row)
        quality = _judge_section_quality(draft, packet, real_llm=real_llm)
        citation_ready = bool(
            real_llm
            and valid_chunk_ids
            and not invalid
            and not uncited
            and not rejected_entailment_sentences
            and draft.status not in {"failed", "audit_failed"}
        )
        required_visuals = list(
            packet.section_contract.get("expected_visual_arguments") or []
        )
        verified_figure_present = any(
            str(item.get("local_image_path") or "")
            and Path(str(item.get("local_image_path"))).exists()
            for item in draft.figure_placements
            if isinstance(item, dict)
        )
        required_visual_missing = bool(required_visuals and not verified_figure_present)
        try:
            word_budget = max(0, int(packet.section_contract.get("word_budget") or 0))
        except (TypeError, ValueError):
            word_budget = 0
        actual_word_count = len(re.findall(r"\b[A-Za-z][A-Za-z'-]*\b", draft.english_text))
        word_budget_compliant, word_budget_compliance_mode, word_budget_ratio = (
            _word_budget_compliance(
                draft,
                word_budget=word_budget,
                actual_word_count=actual_word_count,
            )
        )
        expected_paragraph_count = len(
            packet.section_contract.get("paragraph_functions") or []
        )
        actual_paragraph_count = len([
            row for row in re.split(r"\n\s*\n", draft.english_text) if row.strip()
        ])
        paragraph_contract_compliant = bool(
            not expected_paragraph_count
            or actual_paragraph_count >= (
                1 if expected_paragraph_count == 1
                else max(2, expected_paragraph_count - 1)
            )
        )
        text_fail_closed = bool(
            not real_llm
            or not citation_ready
            or not word_budget_compliant
            or not paragraph_contract_compliant
            or quality.get("unsupported_fact_detected")
            or quality.get("verdict") in {"unusable", "judge_failed"}
        )
        fail_closed = bool(text_fail_closed or required_visual_missing)
        return {
            "section_id": draft.section_id,
            "valid_evidence_chunk_count": len(valid_chunk_ids),
            "accepted_cited_chunk_ids": sorted(cited_chunk_ids & valid_chunk_ids),
            "invalid_cited_chunk_ids": invalid,
            "uncited_load_bearing_claim_ids": uncited,
            "internal_reference_marker_count": len(unresolved_markers),
            "uncited_after_entailment_rejection": rejected_entailment_sentences,
            "citation_ready": citation_ready,
            "required_visual_missing": required_visual_missing,
            "actual_word_count": actual_word_count,
            "word_budget": word_budget,
            "word_budget_ratio": round(word_budget_ratio, 3) if word_budget else None,
            "word_budget_compliant": word_budget_compliant,
            "word_budget_compliance_mode": word_budget_compliance_mode,
            "expected_paragraph_count": expected_paragraph_count,
            "actual_paragraph_count": actual_paragraph_count,
            "paragraph_contract_compliant": paragraph_contract_compliant,
            "section_quality_judgment": quality,
            "text_ready": bool(real_llm and not text_fail_closed),
            "formal_ready": bool(real_llm and not fail_closed),
        }

    if real_llm and len(drafts) > 1:
        with ThreadPoolExecutor(max_workers=min(3, len(drafts))) as pool:
            audits = list(pool.map(audit_one, drafts))
    else:
        audits = [audit_one(draft) for draft in drafts]
    return {
        "schema_version": "full_review.citation_audits.v1",
        "citation_audits": audits,
        "formal_ready_section_count": sum(bool(row.get("formal_ready")) for row in audits),
        "text_ready_section_count": sum(bool(row.get("text_ready")) for row in audits),
        "citation_ready_section_count": sum(bool(row.get("citation_ready")) for row in audits),
        "sections_requiring_revision": [
            row["section_id"] for row in audits if not row.get("formal_ready")
        ],
        "invalid_citation_count": sum(
            len(row.get("invalid_cited_chunk_ids") or []) for row in audits
        ),
        "uncited_load_bearing_claim_count": sum(
            len(row.get("uncited_load_bearing_claim_ids") or []) for row in audits
        ),
        "quality_judge_failure_count": sum(
            str((row.get("section_quality_judgment") or {}).get("verdict") or "")
            == "judge_failed"
            for row in audits
        ),
        "audit_policy": (
            "A citation is structurally valid only when it resolves to a canonical evidence "
            "packet. Text readiness additionally requires load-bearing coverage, section "
            "contract compliance, and an independent quality judgment. Visual completeness "
            "is reported separately and never deletes otherwise valid prose."
        ),
    }


def _m4_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _m4_stop_reason(status: str) -> str:
    return {
        "noop": "m4_noop",
        "awaiting_approval": "awaiting_m4_patch_approval",
        "applied": "m4_patches_applied",
        "rejected": "m4_patches_rejected",
        "failed_qwen": "failed_qwen",
    }.get(status, f"m4_{status}")


def _m4_citation_audit_passed(
    report: dict[str, Any] | None,
    *,
    real_llm: bool,
    section_count: int,
) -> bool:
    """Deterministic post-apply citation gate.

    Structural counts are always authoritative. In real mode the independent
    formal readiness of every section is required as well; an unavailable
    judge is an infrastructure failure and fails the candidate closed.
    """

    if not isinstance(report, dict):
        return False
    if int(report.get("invalid_citation_count") or 0) != 0:
        return False
    if int(report.get("uncited_load_bearing_claim_count") or 0) != 0:
        return False
    if int(report.get("quality_judge_failure_count") or 0) != 0:
        return False
    if real_llm and int(
        report.get("formal_ready_section_count") or 0
    ) != section_count:
        return False
    return True


def _m4_sections_from_bundle(
    draft_bundle: dict[str, Any],
    packet_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    packets_by_id = {
        str(row.get("section_id") or ""): row
        for row in packet_rows
        if isinstance(row, dict)
    }
    sections: list[dict[str, Any]] = []
    for row in draft_bundle.get("section_drafts") or []:
        if not isinstance(row, dict):
            continue
        section_id = str(row.get("section_id") or "")
        if not section_id:
            continue
        sections.append(
            {
                "section_id": section_id,
                "draft_text": str(row.get("english_text") or ""),
                "input_packet": packets_by_id.get(section_id) or {},
            }
        )
    return sections


def _m4_write_snapshot_files(
    snapshot_dir: Path,
    m4_sections: list[dict[str, Any]],
) -> tuple[Path, list[dict[str, Any]]]:
    """Freeze the pre-edit bundle to disk so the original is recoverable."""

    snapshot_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for section in m4_sections:
        section_id = str(section.get("section_id") or "")
        section_dir = snapshot_dir / section_id
        section_dir.mkdir(parents=True, exist_ok=True)
        draft_path = section_dir / "SECTION_DRAFT_EN.md"
        packet_path = section_dir / "input_packet.json"
        draft_path.write_text(
            str(section.get("draft_text") or ""), encoding="utf-8"
        )
        packet_path.write_text(
            json.dumps(
                section.get("input_packet") or {},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        rows.append(
            {
                "section_id": section_id,
                "english_draft_path": str(draft_path),
                "input_packet_path": str(packet_path),
            }
        )
    manifest = {
        "schema_version": "optomind.global_manuscript_commander.manifest.v1",
        "sections": rows,
    }
    manifest_path = snapshot_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest_path, rows


def _m4_load_proposal(path: str | Path | None, fingerprint: str) -> dict[str, Any] | None:
    if not path:
        return None
    proposal_path = Path(path)
    if not proposal_path.is_file():
        return None
    try:
        data = json.loads(proposal_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if str(data.get("schema_version") or "") != M4_PROPOSAL_SCHEMA_VERSION:
        return None
    if str(data.get("base_fingerprint") or "") != str(fingerprint or ""):
        return None
    if str(data.get("status") or "") != "completed":
        return None
    return data


def _m4_save_proposal(path: str | Path, proposal: dict[str, Any]) -> None:
    proposal_path = Path(path)
    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    proposal_path.write_text(
        json.dumps(proposal, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def edit_cross_section(
    draft_bundle: dict[str, Any],
    citation_bundle: dict[str, Any],
    *,
    real_llm: bool,
    approvals: dict[str, str] | None = None,
    m4_snapshot_dir: str | Path | None = None,
    m4_diagnostics_dir: str | Path | None = None,
    m4_proposal_path: str | Path | None = None,
    m4_role_provider: Any = None,
    model_tier: str = "premium_model",
) -> dict[str, Any]:
    """Implement S15 under the M4 integration contract.

    Pipeline: freeze snapshot -> deterministic assembly -> Qwen issue/patch
    proposal -> patch safety gate -> apply to new version only -> rebuild
    citation and claim/evidence ledger. Qwen owns semantic/global editorial
    judgment; rules own hashes, allowlists, schema validation, deterministic
    mechanics, audit reports, and rejection. S15 never performs a
    whole-section or full-manuscript free rewrite.
    """

    from optomind_research.runtime.global_manuscript_commander import (
        apply_m4_patch_set,
        audit_m4_claim_evidence_ledger,
        build_m4_snapshot,
        compute_fingerprint,
        run_global_manuscript_commander,
        validate_m4_patch_set,
    )

    blueprint = copy.deepcopy(draft_bundle.get("blueprint") or {})
    original_draft_rows = [
        dict(row)
        for row in (draft_bundle.get("section_drafts") or [])
        if isinstance(row, dict)
    ]
    packet_rows = [
        dict(row)
        for row in (draft_bundle.get("material_packets") or [])
        if isinstance(row, dict)
    ]
    drafts = [draft_from_dict(row) for row in original_draft_rows]
    packets = [packet_from_dict(row) for row in packet_rows]
    before = {draft.section_id: draft.english_text for draft in drafts}
    m4_sections = _m4_sections_from_bundle(draft_bundle, packet_rows)
    snapshot_dir = (
        Path(m4_snapshot_dir)
        if m4_snapshot_dir
        else Path(tempfile.mkdtemp(prefix="m4_snapshot_"))
    )
    diagnostics_dir = (
        Path(m4_diagnostics_dir)
        if m4_diagnostics_dir
        else Path(tempfile.mkdtemp(prefix="m4_diagnostics_"))
    )
    manifest_path, manifest_rows = _m4_write_snapshot_files(
        snapshot_dir, m4_sections
    )
    fingerprint = compute_fingerprint(manifest_path, manifest_rows, PROMPTS_DIR)
    snapshot = build_m4_snapshot(m4_sections, fingerprint=fingerprint)
    (snapshot_dir / "m4_snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    pre_edit_bundle = {
        "schema_version": "full_review.m4_pre_edit_freeze.v1",
        "frozen_at": _m4_utc_now(),
        "base_fingerprint": fingerprint,
        "base_snapshot_hash": snapshot.get("base_snapshot_hash") or "",
        "blueprint": blueprint,
        "section_drafts": original_draft_rows,
        "material_packets": packet_rows,
        "pre_edit_citation_audit": citation_bundle,
    }
    pre_edit_path = snapshot_dir / "pre_edit_bundle.json"
    pre_edit_path.write_text(
        json.dumps(pre_edit_bundle, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    proposal = _m4_load_proposal(m4_proposal_path, fingerprint)
    proposal_source = "resumed" if proposal is not None else "generated"
    commander_status = "completed"
    commander_error = ""
    if proposal is None:
        commander_summary = run_global_manuscript_commander(
            manifest_path=manifest_path,
            output_dir=diagnostics_dir,
            model_tier=model_tier,
            live=real_llm,
            role_provider=m4_role_provider,
            resume=False,
        )
        commander_status = str(commander_summary.get("status") or "failed")
        commander_error = str(commander_summary.get("error") or "")
        work_order: dict[str, Any] = {}
        work_order_path = diagnostics_dir / "global_commander_work_order.json"
        if work_order_path.is_file():
            try:
                loaded = json.loads(work_order_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    work_order = loaded
            except (OSError, ValueError, TypeError):
                work_order = {}
        proposal = {
            "schema_version": M4_PROPOSAL_SCHEMA_VERSION,
            "status": commander_status,
            "base_fingerprint": fingerprint,
            "base_snapshot_hash": snapshot.get("base_snapshot_hash") or "",
            "mode": "live" if real_llm else "dry",
            "model_tier": model_tier,
            "generated_at": _m4_utc_now(),
            "commander_diagnostics_dir": str(diagnostics_dir.resolve()),
            "proposed_patch_set": list(
                work_order.get("proposed_patch_set") or []
            ),
        }
        if m4_proposal_path and commander_status == "completed":
            _m4_save_proposal(m4_proposal_path, proposal)

    if commander_status != "completed":
        m4_status = "failed_qwen"
        validation = {
            "schema_version": (
                "optomind.global_manuscript_commander.m4_patch_set.v1"
            ),
            "status": "rejected",
            "errors": [
                commander_error
                or "live Qwen patch proposer unavailable; no deterministic "
                "scientific decision was substituted"
            ],
            "warnings": [],
            "patch_reports": [],
            "valid_patches": [],
            "awaiting_patches": [],
            "declined_patches": [],
            "rejected_patches": [],
        }
        apply_result = None
    else:
        patch_set = list(proposal.get("proposed_patch_set") or [])
        expected_hash = str(proposal.get("base_snapshot_hash") or "")
        validation = validate_m4_patch_set(
            snapshot,
            patch_set,
            approvals,
            expected_snapshot_hash=expected_hash,
        )
        if validation["status"] == "rejected":
            m4_status = "rejected"
            apply_result = None
        elif validation["status"] == "awaiting_approval":
            m4_status = "awaiting_approval"
            apply_result = None
        else:
            apply_result = apply_m4_patch_set(
                snapshot,
                patch_set,
                approvals,
                expected_snapshot_hash=expected_hash,
            )
            m4_status = str(apply_result.get("status") or "noop")

    new_text_by_section = (
        (apply_result or {}).get("new_text_by_section") or {}
    )
    applied_patch_ids = [
        row.get("patch_id")
        for row in ((apply_result or {}).get("applied_patches") or [])
    ]
    candidate_drafts: list[dict[str, Any]] = []
    changed: list[str] = []
    for row in original_draft_rows:
        output = copy.deepcopy(row)
        section_id = str(output.get("section_id") or "")
        new_text = new_text_by_section.get(section_id)
        if new_text is not None and new_text != str(output.get("english_text") or ""):
            output["english_text"] = new_text
            if output.get("status") != "failed":
                output["status"] = "cross_section_edited"
            history = list(output.get("revision_history") or [])
            history.append(
                {
                    "stage": "S15_m4_patch_apply",
                    "accepted": True,
                    "patch_ids": list(applied_patch_ids),
                    "base_snapshot_hash": snapshot.get("base_snapshot_hash") or "",
                    "post_snapshot_hash": (
                        (apply_result or {}).get("post_snapshot_hash") or ""
                    ),
                    "created_at": _m4_utc_now(),
                }
            )
            output["revision_history"] = history
            changed.append(section_id)
        candidate_drafts.append(output)

    rollback_report = None
    failed_candidate_audits = None
    post_apply_citation_audit = None
    post_apply_ledger_audit = None
    post_apply_continuity_audit = None
    if m4_status == "applied":
        candidate_objects = [draft_from_dict(row) for row in candidate_drafts]
        candidate_packets = [packet_from_dict(row) for row in packet_rows]
        candidate_continuity = audit_manuscript_continuity(
            candidate_objects, candidate_packets
        )
        candidate_bundle = {
            "schema_version": "full_review.section_drafts.v1",
            "blueprint": blueprint,
            "section_drafts": candidate_drafts,
            "material_packets": packet_rows,
        }
        citation_audit = audit_citations(
            candidate_bundle, real_llm=real_llm
        )
        ledger_audit = audit_m4_claim_evidence_ledger(
            snapshot, new_text_by_section
        )
        audit_failures: list[str] = []
        if not _m4_citation_audit_passed(
            citation_audit,
            real_llm=real_llm,
            section_count=len(candidate_objects),
        ):
            audit_failures.append("citation_audit")
        if str(ledger_audit.get("status") or "") != "passed":
            audit_failures.append("claim_evidence_ledger")
        if not bool(candidate_continuity.get("passed")):
            audit_failures.append("continuity")
        if audit_failures:
            # Fail closed: reject the candidate and restore the frozen
            # original byte-for-byte. S16 must never see candidate text.
            rollback_report = {
                "schema_version": "full_review.m4_rollback_report.v1",
                "status": "rolled_back",
                "attempted_patch_ids": list(applied_patch_ids),
                "candidate_post_snapshot_hash": (
                    (apply_result or {}).get("post_snapshot_hash") or ""
                ),
                "restored_base_snapshot_hash": (
                    snapshot.get("base_snapshot_hash") or ""
                ),
                "audit_failures": audit_failures,
                "byte_identical_restoration": True,
                "recoverable_snapshot_path": str(pre_edit_path.resolve()),
                "candidate_audit_reports_preserved": True,
            }
            failed_candidate_audits = {
                "post_apply_citation_audit": citation_audit,
                "post_apply_claim_evidence_audit": ledger_audit,
                "post_apply_continuity_audit": candidate_continuity,
            }
            candidate_drafts = [
                copy.deepcopy(row) for row in original_draft_rows
            ]
            changed = []
            m4_status = "rejected"
            post_apply_citation_audit = citation_audit
            post_apply_ledger_audit = ledger_audit
            post_apply_continuity_audit = candidate_continuity
        else:
            post_apply_citation_audit = citation_audit
            post_apply_ledger_audit = ledger_audit
            post_apply_continuity_audit = candidate_continuity

    output_drafts = candidate_drafts
    output_objects = [draft_from_dict(row) for row in output_drafts]
    output_packets = [packet_from_dict(row) for row in packet_rows]
    review = _review_text(output_objects, blueprint)
    continuity_audit = audit_manuscript_continuity(
        output_objects, output_packets
    )
    post_snapshot_hash = (
        snapshot.get("base_snapshot_hash") or ""
        if rollback_report
        else (apply_result or {}).get("post_snapshot_hash")
        or snapshot.get("base_snapshot_hash")
        or ""
    )
    stop_reason = (
        "m4_post_apply_audit_failed"
        if rollback_report
        else _m4_stop_reason(m4_status)
    )

    m4_contract = {
        "schema_version": M4_CONTRACT_SCHEMA_VERSION,
        "status": m4_status,
        "stop_reason": stop_reason,
        "mode": "live" if real_llm else "dry",
        "model_tier": model_tier,
        "base_fingerprint": fingerprint,
        "base_snapshot_hash": snapshot.get("base_snapshot_hash") or "",
        "post_snapshot_hash": post_snapshot_hash,
        "proposal_source": proposal_source,
        "proposal": proposal,
        "validation": validation,
        "apply_report": apply_result,
        "rollback_report": rollback_report,
        "failed_candidate_audits": failed_candidate_audits,
        "decisions": dict(approvals or {}),
        "post_apply_citation_audit": post_apply_citation_audit,
        "post_apply_claim_evidence_audit": post_apply_ledger_audit,
        "post_apply_continuity_audit": post_apply_continuity_audit,
        "commander": {
            "status": commander_status,
            "error": commander_error,
            "diagnostics_dir": str(
                proposal.get("commander_diagnostics_dir")
                or diagnostics_dir.resolve()
            ),
        },
        "original_bundle": {
            "pre_edit_bundle_path": str(pre_edit_path.resolve()),
            "snapshot_dir": str(snapshot_dir.resolve()),
            "recoverable": True,
        },
        "policy": (
            "S15 never performs a whole-section or full-manuscript rewrite. "
            "Qwen proposes a structured patch set against frozen block hashes; "
            "rules validate IDs, hashes, claims/evidence, authorization, and "
            "move ownership/boundary compliance, apply explicitly authorized "
            "deterministic patches to a candidate in-memory version only, "
            "rerun citation, claim/evidence, and continuity audits, and "
            "commit the candidate or restore the frozen original "
            "byte-for-byte with a rollback report."
        ),
    }
    return {
        "schema_version": "full_review.cross_section_edit.v2",
        "blueprint": blueprint,
        "section_drafts": output_drafts,
        "material_packets": packet_rows,
        "full_review_english": review,
        "pre_edit_citation_audit": citation_bundle,
        "manuscript_continuity_audit": continuity_audit,
        "changed_section_ids": changed,
        "m4_apply_status": m4_status,
        "m4_contract": m4_contract,
        "quality_summary": {
            "section_count": len(output_objects),
            "changed_section_count": len(changed),
            "failed_section_ids": [
                draft.section_id
                for draft in output_objects
                if draft.status == "failed"
            ],
            "post_edit_citation_count": sum(
                len(ids)
                for draft in output_objects
                for ids in draft.citation_map.values()
            ),
            "english_word_count": len(
                re.findall(r"\b[A-Za-z][A-Za-z'-]*\b", review)
            ),
            "continuity_major_issue_count": int(
                continuity_audit.get("major_count") or 0
            ),
            "continuity_minor_issue_count": int(
                continuity_audit.get("minor_count") or 0
            ),
            "manuscript_continuity_passed": bool(
                continuity_audit.get("passed")
            ),
            "m4_status": m4_status,
            "m4_applied_patch_count": len(
                (apply_result or {}).get("applied_patches") or []
            ),
            "m4_rejected_patch_count": len(
                (validation or {}).get("rejected_patches") or []
            ),
            "m4_awaiting_patch_count": len(
                (validation or {}).get("awaiting_patches") or []
            ),
        },
    }


def run_supervisor_review(
    edit_bundle: dict[str, Any],
    *,
    real_llm: bool,
    decisions: dict[str, str] | None = None,
    charter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Implement S16. The supervisor proposes; this function never rewrites prose."""
    from optomind_research.supervisor import Supervisor

    blueprint = edit_bundle.get("blueprint") or {}
    drafts = [draft_from_dict(row) for row in (edit_bundle.get("section_drafts") or [])]
    claims_by_section = {
        str(section.get("section_id") or ""): list(section.get("claims") or [])
        for section in (blueprint.get("sections") or [])
    }
    # Prefer the write-authorized claim view.  Material packets carry the
    # supported rewrite, missing components, and writing permission that the
    # prose actually obeyed; raw blueprint claims may still contain clauses
    # deliberately excluded by the evidence verifier.
    for row in (edit_bundle.get("material_packets") or []):
        if isinstance(row, dict) and row.get("section_id"):
            claims_by_section[str(row["section_id"])] = list(row.get("claims") or [])
    charter = charter or {}
    evaluation_context = dict(charter.get("evaluation_context") or {})
    audit_by_section = {
        str(row.get("section_id") or ""): row
        for row in (
            (edit_bundle.get("pre_edit_citation_audit") or {}).get("citation_audits")
            or []
        )
        if isinstance(row, dict) and row.get("section_id")
    }
    supervisor = Supervisor(model_tier="premium_model", real_llm=real_llm)
    supervisor.review_blueprint(
        blueprint,
        run_manifest_summary={
            "review_title": charter.get("title") or charter.get("review_title") or "",
            "central_question": charter.get("central_question") or "",
            "scope_statement": charter.get("scope_statement") or "",
            "evaluation_context": evaluation_context,
        },
    )
    def review_one_section(draft: SectionDraft) -> list[Any]:
        local = Supervisor(model_tier="premium_model", real_llm=real_llm)
        section_context = dict(evaluation_context)
        if draft.section_id in audit_by_section:
            section_context["independent_section_audit"] = audit_by_section[draft.section_id]
        return local.review_section_draft(
            section_id=draft.section_id,
            draft_text=draft.english_text,
            claims=claims_by_section.get(draft.section_id, []),
            citation_map=draft.citation_map,
            overclaim_flags=draft.overclaim_flags,
            evaluation_context=section_context,
        )

    if real_llm and len(drafts) > 1:
        indexed_results: dict[int, list[Any]] = {}
        with ThreadPoolExecutor(max_workers=min(4, len(drafts))) as pool:
            futures = {
                pool.submit(review_one_section, draft): index
                for index, draft in enumerate(drafts)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    indexed_results[index] = future.result()
                except Exception:
                    indexed_results[index] = []
        for index in range(len(drafts)):
            supervisor.suggestions.extend(indexed_results.get(index, []))
    else:
        for draft in drafts:
            supervisor.suggestions.extend(review_one_section(draft))
    decision_map = {str(k): str(v).lower() for k, v in (decisions or {}).items()}
    for suggestion in supervisor.suggestions:
        decision = decision_map.get(suggestion.suggestion_id)
        if decision == "accepted":
            supervisor.accept_suggestion(suggestion.suggestion_id, operator="human")
        elif decision == "rejected":
            supervisor.reject_suggestion(suggestion.suggestion_id, operator="human")
    payload = supervisor.to_dict()
    payload.update({
        "schema_version": "full_review.supervisor_review.v1",
        "decision_policy": (
            "Supervisor suggestions never change formal artifacts until their suggestion_id "
            "is explicitly accepted."
        ),
    })
    return payload


def _apply_safe_claim_updates(
    blueprint: dict[str, Any],
    updates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    claims = {
        str(claim.get("claim_id") or ""): claim
        for section in (blueprint.get("sections") or [])
        for claim in (section.get("claims") or [])
        if isinstance(claim, dict) and claim.get("claim_id")
    }
    accepted: list[dict[str, Any]] = []
    for update in updates:
        claim = claims.get(str(update.get("claim_id") or ""))
        requirement = str(update.get("evidence_requirement") or "").lower()
        state = str(update.get("claim_state") or "").lower()
        expected = {"open_question": "open_question", "normative": "reframed"}.get(requirement)
        if claim is None or state != expected:
            continue
        claim["original_statement"] = str(
            claim.get("original_statement") or claim.get("statement") or ""
        )
        claim["evidence_requirement"] = requirement
        claim["claim_state"] = state
        claim["load_bearing"] = False
        claim["closure_disposition"] = (
            "open_question" if requirement == "open_question" else "recommendation"
        )
        accepted.append(dict(update))
    return accepted


def apply_feedback_revision(
    edit_bundle: dict[str, Any],
    supervisor_bundle: dict[str, Any],
    *,
    real_llm: bool,
) -> dict[str, Any]:
    """Implement S17 using accepted supervisor suggestions only."""
    blueprint = copy.deepcopy(edit_bundle.get("blueprint") or {})
    drafts = [draft_from_dict(row) for row in (edit_bundle.get("section_drafts") or [])]
    packets = [packet_from_dict(row) for row in (edit_bundle.get("material_packets") or [])]
    packet_by_id = {packet.section_id: packet for packet in packets}
    section_claim_ids = {
        str(section.get("section_id") or ""): {
            str(claim.get("claim_id") or "") for claim in (section.get("claims") or [])
        }
        for section in (blueprint.get("sections") or [])
    }
    accepted_suggestions = [
        row for row in (supervisor_bundle.get("suggestions") or [])
        if isinstance(row, dict) and row.get("status") == "accepted"
    ]
    reviser = EvidenceAwareRevisionAgent(model_tier="premium_model", real_llm=real_llm)
    binder = CitationBinder(model_tier="premium_model", real_llm=real_llm)
    auditor = OverclaimAuditor(model_tier="advanced_model", real_llm=real_llm)
    revision_rows: list[dict[str, Any]] = []
    for draft in drafts:
        packet = packet_by_id.get(draft.section_id)
        if packet is None:
            continue
        targets = {draft.section_id} | section_claim_ids.get(draft.section_id, set())
        relevant = [
            row for row in accepted_suggestions if str(row.get("target_id") or "") in targets
        ]
        before = draft.english_text
        if relevant:
            draft = reviser.revise(draft, packet, relevant)
            latest = draft.revision_history[-1] if draft.revision_history else {}
            safe_updates = _apply_safe_claim_updates(
                blueprint, list(latest.get("claim_state_updates") or [])
            )
            # Keep the compact writer/audit view in sync with the canonical
            # blueprint.  Otherwise a human-approved conversion to an open
            # question would be visible in the blueprint but the post-revision
            # citation audit would still treat the stale packet copy as a
            # load-bearing factual claim.
            if safe_updates:
                _apply_safe_claim_updates(
                    {"sections": [{"claims": packet.claims}]}, safe_updates
                )
            if draft.english_text != before:
                draft = binder.bind(draft, packet)
                audited_before = draft.english_text
                draft = auditor.audit(draft, packet)
                if draft.english_text != audited_before:
                    draft = binder.bind(draft, packet)
            revision_rows.append({
                "section_id": draft.section_id,
                "accepted_suggestion_ids": [str(row.get("suggestion_id")) for row in relevant],
                "text_changed": draft.english_text != before,
                "safe_claim_state_updates": safe_updates,
                "revision_accepted_by_safety_gate": bool(
                    draft.revision_history and draft.revision_history[-1].get("accepted")
                ),
            })
    unhandled = [
        row for row in accepted_suggestions
        if not any(
            str(row.get("suggestion_id")) in item.get("accepted_suggestion_ids", [])
            for item in revision_rows
        )
    ]
    review = _review_text(drafts, blueprint)
    return {
        "schema_version": "full_review.feedback_revision.v1",
        "blueprint": blueprint,
        "section_drafts": [draft_to_dict(draft) for draft in drafts],
        "material_packets": [packet.to_dict() for packet in packets],
        "full_review_english": review,
        "revision_report": revision_rows,
        "accepted_suggestion_count": len(accepted_suggestions),
        "unhandled_accepted_suggestions": unhandled,
        "supervisor_status_summary": dict(supervisor_bundle.get("status_summary") or {}),
        "pending_supervisor_suggestions": [
            row for row in (supervisor_bundle.get("suggestions") or [])
            if isinstance(row, dict) and row.get("status") == "pending"
        ],
        "stop_reason": (
            "no_approved_suggestions"
            if not accepted_suggestions
            else "all_applicable_approved_suggestions_processed"
            if not unhandled
            else "some_approved_suggestions_require_human_or_upstream_revision"
        ),
    }


def run_global_review(
    revision_bundle: dict[str, Any],
    *,
    charter: dict[str, Any],
    contracts: list[dict[str, Any]],
    citation_bundle: dict[str, Any],
    real_llm: bool,
) -> dict[str, Any]:
    """Implement S18: whole-manuscript argument and readiness audit."""
    blueprint = revision_bundle.get("blueprint") or {}
    review_drafts = [
        remove_broken_visual_promises(draft_from_dict(row))
        for row in (revision_bundle.get("section_drafts") or [])
    ]
    review = _review_text(review_drafts, blueprint)
    deterministic = {
        "section_count": len(review_drafts),
        "english_word_count": len(re.findall(r"\b[A-Za-z][A-Za-z'-]*\b", review)),
        "invalid_citation_count": int(citation_bundle.get("invalid_citation_count") or 0),
        "uncited_load_bearing_claim_count": int(
            citation_bundle.get("uncited_load_bearing_claim_count") or 0
        ),
        "citation_ready_section_count": int(
            citation_bundle.get("citation_ready_section_count") or 0
        ),
        "formal_ready_section_count": int(
            citation_bundle.get("formal_ready_section_count") or 0
        ),
        "required_visual_missing_section_count": sum(
            bool(row.get("required_visual_missing"))
            for row in (citation_bundle.get("citation_audits") or [])
            if isinstance(row, dict)
        ),
        "word_budget_noncompliant_section_count": sum(
            not bool(row.get("word_budget_compliant", True))
            for row in (citation_bundle.get("citation_audits") or [])
            if isinstance(row, dict)
        ),
        "paragraph_contract_noncompliant_section_count": sum(
            not bool(row.get("paragraph_contract_compliant", True))
            for row in (citation_bundle.get("citation_audits") or [])
            if isinstance(row, dict)
        ),
        "empty_section_count": sum(
            not str(row.get("english_text") or "").strip()
            for row in [draft_to_dict(draft) for draft in review_drafts]
        ),
    }
    if not real_llm:
        judgment = {
            "scores": {},
            "overall_score": 0,
            "verdict": "mock_not_evaluated",
            "strengths": [],
            "issues": [],
            "formal_readiness": "needs_revision",
            "highest_value_next_action": "Run the real global review before formal use.",
        }
    else:
        compact_citation_audit = {
            "formal_ready_section_count": int(
                citation_bundle.get("formal_ready_section_count") or 0
            ),
            "citation_ready_section_count": int(
                citation_bundle.get("citation_ready_section_count") or 0
            ),
            "invalid_citation_count": int(citation_bundle.get("invalid_citation_count") or 0),
            "uncited_load_bearing_claim_count": int(
                citation_bundle.get("uncited_load_bearing_claim_count") or 0
            ),
            "sections": [
                {
                    "section_id": row.get("section_id"),
                    "citation_ready": bool(row.get("citation_ready")),
                    "formal_ready": bool(row.get("formal_ready")),
                    "required_visual_missing": bool(row.get("required_visual_missing")),
                    "word_budget_compliant": bool(row.get("word_budget_compliant", True)),
                    "word_budget_ratio": row.get("word_budget_ratio"),
                    "paragraph_contract_compliant": bool(
                        row.get("paragraph_contract_compliant", True)
                    ),
                    "invalid_cited_chunk_ids": list(row.get("invalid_cited_chunk_ids") or []),
                    "uncited_load_bearing_claim_ids": list(
                        row.get("uncited_load_bearing_claim_ids") or []
                    ),
                    "uncited_after_entailment_rejection_count": len(
                        row.get("uncited_after_entailment_rejection") or []
                    ),
                    "quality_scores": dict(
                        (row.get("section_quality_judgment") or {}).get("scores") or {}
                    ),
                    "quality_verdict": str(
                        (row.get("section_quality_judgment") or {}).get("verdict") or ""
                    ),
                    "unsupported_fact_detected": bool(
                        (row.get("section_quality_judgment") or {}).get(
                            "unsupported_fact_detected"
                        )
                    ),
                }
                for row in (citation_bundle.get("citation_audits") or [])
                if isinstance(row, dict)
            ],
        }
        payload = {
            "review_charter": charter,
            "section_contracts": contracts,
            "citation_audit_summary": compact_citation_audit,
            "deterministic_metrics": deterministic,
            "full_english_manuscript": review,
        }
        messages = [
            {"role": "system", "content": GLOBAL_REVIEW_PROMPT.read_text(encoding="utf-8")},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        judgment = {}
        review_attempts: list[dict[str, Any]] = []
        for attempt, timeout_seconds in enumerate((150, 180), start=1):
            try:
                result = call_qwen_chat(
                    "GlobalReviewJudge" if attempt == 1 else "GlobalReviewJudgeRetry",
                    messages,
                    model_tier="premium_model",
                    temperature=0,
                    max_tokens=3200,
                    response_format={"type": "json_object"},
                    force_mock=False,
                    max_retries=0,
                    timeout_seconds=timeout_seconds,
                    max_transport_key_candidates=1,
                    allow_model_fallback=False,
                )
                judgment = _safe_json(str(result.get("content") or ""))
                review_attempts.append({
                    "attempt": attempt,
                    "valid_json": bool(judgment),
                    "error_type": str((result.get("_llm_usage") or {}).get("error_type") or ""),
                })
            except Exception as exc:
                review_attempts.append({
                    "attempt": attempt,
                    "valid_json": False,
                    "error_type": type(exc).__name__,
                })
                judgment = {}
            if judgment:
                break
        if not judgment:
            judgment = {
                "scores": {},
                "overall_score": 0,
                "verdict": "judge_failed",
                "strengths": [],
                "issues": [{
                    "issue_id": "GR-ERROR",
                    "severity": "high",
                    "section_ids": [],
                    "issue_type": "argument_gap",
                    "description": "The global review judge returned invalid output.",
                    "recommended_action": "Repeat the global review audit.",
                }],
                "formal_readiness": "blocked",
                "highest_value_next_action": "Repeat the global review audit.",
            }
        judgment["review_process"] = {
            "attempt_count": len(review_attempts),
            "valid_output": not any(
                str(row.get("issue_id") or "") == "GR-ERROR"
                for row in (judgment.get("issues") or []) if isinstance(row, dict)
            ),
            "attempts": review_attempts,
        }
        judgment = _normalize_five_point_scores(judgment)
    issues = [row for row in (judgment.get("issues") or []) if isinstance(row, dict)]
    return {
        "schema_version": "full_review.global_review.v1",
        "deterministic_metrics": deterministic,
        "judgment": judgment,
        "high_or_critical_issue_count": sum(
            str(row.get("severity") or "") in {"high", "critical"} for row in issues
        ),
        "critical_issue_count": sum(
            str(row.get("severity") or "") == "critical" for row in issues
        ),
    }


PEER_REVIEWER_ROLES: tuple[tuple[str, str], ...] = (
    (
        "domain_science_reviewer",
        "Judge physical reasoning, scope, mechanism explanations, boundary conditions, and fair comparison.",
    ),
    (
        "evidence_and_provenance_reviewer",
        "Judge whether load-bearing claims, citations, uncertainty labels, and source boundaries are defensible.",
    ),
    (
        "top_review_editor",
        "Judge thesis control, synthesis depth, section architecture, transitions, and value beyond paper listing.",
    ),
    (
        "multimodal_synthesis_reviewer",
        "Judge whether figures and visual evidence advance the argument, remain traceable, and avoid decorative use.",
    ),
)


def run_peer_review_panel(
    revision_bundle: dict[str, Any],
    global_bundle: dict[str, Any],
    *,
    charter: dict[str, Any],
    real_llm: bool,
) -> dict[str, Any]:
    """Implement S19 with independent adversarial reviewer roles."""
    if not real_llm:
        reviews = [
            {
                "reviewer_role": role,
                "recommendation": "major_revision",
                "confidence": "low",
                "strengths": [],
                "issues": [],
                "questions_for_authors": [],
                "publication_blockers": ["Real peer review was not run in mock mode."],
            }
            for role, _ in PEER_REVIEWER_ROLES
        ]
    else:
        system = PEER_REVIEW_PROMPT.read_text(encoding="utf-8")
        review_drafts = [
            remove_broken_visual_promises(draft_from_dict(row))
            for row in (revision_bundle.get("section_drafts") or [])
        ]
        review_text = _review_text(
            review_drafts, revision_bundle.get("blueprint") or {}
        )
        post_audit = global_bundle.get("post_revision_citation_audit") or {}
        independent_audit_summary = {
            "deterministic_metrics": dict(global_bundle.get("deterministic_metrics") or {}),
            "formal_ready_section_count": int(
                post_audit.get("formal_ready_section_count") or 0
            ),
            "citation_ready_section_count": int(
                post_audit.get("citation_ready_section_count") or 0
            ),
            "invalid_citation_count": int(post_audit.get("invalid_citation_count") or 0),
            "uncited_load_bearing_claim_count": int(
                post_audit.get("uncited_load_bearing_claim_count") or 0
            ),
            "uncited_after_entailment_rejection_count": sum(
                len(row.get("uncited_after_entailment_rejection") or [])
                for row in (post_audit.get("citation_audits") or [])
                if isinstance(row, dict)
            ),
            "unsupported_fact_section_count": sum(
                bool(
                    (row.get("section_quality_judgment") or {}).get(
                        "unsupported_fact_detected"
                    )
                )
                for row in (post_audit.get("citation_audits") or [])
                if isinstance(row, dict)
            ),
            "section_status": [
                {
                    "section_id": row.get("section_id"),
                    "citation_ready": bool(row.get("citation_ready")),
                    "formal_ready": bool(row.get("formal_ready")),
                    "required_visual_missing": bool(row.get("required_visual_missing")),
                    "word_budget_compliant": bool(row.get("word_budget_compliant", True)),
                    "word_budget_ratio": row.get("word_budget_ratio"),
                    "paragraph_contract_compliant": bool(
                        row.get("paragraph_contract_compliant", True)
                    ),
                    "invalid_citation_count": len(row.get("invalid_cited_chunk_ids") or []),
                    "uncited_load_bearing_claim_count": len(
                        row.get("uncited_load_bearing_claim_ids") or []
                    ),
                    "uncited_after_entailment_rejection_count": len(
                        row.get("uncited_after_entailment_rejection") or []
                    ),
                    "quality_verdict": str(
                        (row.get("section_quality_judgment") or {}).get("verdict") or ""
                    ),
                    "unsupported_fact_detected": bool(
                        (row.get("section_quality_judgment") or {}).get(
                            "unsupported_fact_detected"
                        )
                    ),
                }
                for row in (post_audit.get("citation_audits") or [])
                if isinstance(row, dict)
            ],
        }

        def evaluate(role_focus: tuple[str, str]) -> dict[str, Any]:
            role, focus = role_focus
            payload = {
                "reviewer_role": role,
                "reviewer_focus": focus,
                "review_charter": charter,
                "independent_deterministic_audit": independent_audit_summary,
                "full_english_manuscript": review_text,
            }
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
            review_attempts: list[dict[str, Any]] = []
            for attempt, timeout_seconds in enumerate((150, 180), start=1):
                try:
                    result = call_qwen_chat(
                        (
                            f"PeerReviewPanel:{role}"
                            if attempt == 1 else f"PeerReviewPanelRetry:{role}"
                        ),
                        messages,
                        model_tier="premium_model",
                        temperature=0.1,
                        max_tokens=2600,
                        response_format={"type": "json_object"},
                        force_mock=False,
                        max_retries=0,
                        timeout_seconds=timeout_seconds,
                        max_transport_key_candidates=1,
                        allow_model_fallback=False,
                    )
                    parsed = _safe_json(str(result.get("content") or ""))
                    review_attempts.append({
                        "attempt": attempt,
                        "valid_json": bool(parsed),
                        "error_type": str(
                            (result.get("_llm_usage") or {}).get("error_type") or ""
                        ),
                    })
                except Exception as exc:
                    parsed = {}
                    review_attempts.append({
                        "attempt": attempt,
                        "valid_json": False,
                        "error_type": type(exc).__name__,
                    })
                if parsed:
                    parsed["reviewer_role"] = role
                    parsed["review_process"] = {
                        "attempt_count": len(review_attempts),
                        "valid_output": True,
                        "attempts": review_attempts,
                    }
                    return parsed
            return {
                "reviewer_role": role,
                "recommendation": "major_revision",
                "confidence": "low",
                "strengths": [],
                "issues": [{
                    "issue_id": f"{role}-ERROR",
                    "severity": "high",
                    "section_ids": [],
                    "issue_type": "review_process_error",
                    "description": "This peer reviewer returned invalid output.",
                    "recommended_action": "Repeat this peer-review role.",
                }],
                "questions_for_authors": [],
                "publication_blockers": ["Peer-review role failed."],
                "review_process": {
                    "attempt_count": len(review_attempts),
                    "valid_output": False,
                    "attempts": review_attempts,
                },
            }

        with ThreadPoolExecutor(max_workers=len(PEER_REVIEWER_ROLES)) as pool:
            reviews = list(pool.map(evaluate, PEER_REVIEWER_ROLES))

        # Enforce the panel's shared severity policy after independent calls.
        # A reviewer can reasonably regard a missing figure as publication-
        # blocking for a top-tier venue, but absence alone is not a critical
        # scientific-integrity failure when the prose remains auditable.  This
        # normalization prevents one role's rhetorical use of "critical" from
        # incorrectly changing the whole pipeline status to a hard block.
        for review in reviews:
            for issue in (review.get("issues") or []):
                if not isinstance(issue, dict):
                    continue
                if (
                    str(issue.get("issue_type") or "").lower() == "visual"
                    and str(issue.get("severity") or "").lower() == "critical"
                ):
                    description = str(issue.get("description") or "").lower()
                    integrity_failure = any(
                        phrase in description
                        for phrase in (
                            "fabricated visual",
                            "falsified visual",
                            "misleading visual",
                            "contradictory visual evidence",
                            "central claim cannot be checked",
                        )
                    )
                    if not integrity_failure:
                        issue["severity"] = "high"
                        issue["severity_calibration"] = (
                            "Missing required visual capped at high: no visual "
                            "fabrication or central scientific-integrity failure was reported."
                        )
    issues = [
        issue
        for review in reviews
        for issue in (review.get("issues") or [])
        if isinstance(issue, dict)
    ]
    return {
        "schema_version": "full_review.peer_reviews.v1",
        "peer_reviews": reviews,
        "recommendation_distribution": {
            recommendation: sum(
                str(review.get("recommendation") or "") == recommendation for review in reviews
            )
            for recommendation in ("accept", "minor_revision", "major_revision", "reject")
        },
        "high_or_critical_issue_count": sum(
            str(issue.get("severity") or "") in {"high", "critical"} for issue in issues
        ),
        "critical_issue_count": sum(
            str(issue.get("severity") or "") == "critical" for issue in issues
        ),
        "panel_policy": (
            "Independent roles review the same immutable manuscript; no reviewer may silently rewrite it."
        ),
    }


def _paper_rows_for_chunks(sqlite_path: Path | None, chunk_ids: list[str]) -> list[dict[str, Any]]:
    if sqlite_path is None or not chunk_ids:
        return []
    connection = sqlite3.connect(str(sqlite_path))
    connection.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in chunk_ids)
        rows = connection.execute(
            f"SELECT DISTINCT p.paper_id,p.doi,p.title,p.year,p.venue,p.raw_json "
            f"FROM text_chunks t JOIN papers p ON p.paper_id=t.paper_id "
            f"WHERE t.chunk_id IN ({placeholders}) ORDER BY p.year,p.title",
            chunk_ids,
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                raw = json.loads(str(item.pop("raw_json") or "{}"))
            except Exception:
                raw = {}
            authors = raw.get("authors") or raw.get("author") or []
            if isinstance(authors, str):
                author_text = authors
            elif isinstance(authors, list):
                author_text = ", ".join(
                    str(author.get("name") or author.get("display_name") or "")
                    if isinstance(author, dict) else str(author)
                    for author in authors[:8]
                )
            else:
                author_text = ""
            item["authors"] = compact(author_text, 500)
            result.append(item)
        return result
    finally:
        connection.close()


def _replace_internal_markers(
    text: str,
    packets: list[SectionMaterialPacket],
    reference_number_by_paper: dict[str, int],
) -> tuple[str, list[str]]:
    value = str(text or "")
    replacements: dict[str, str] = {}
    for packet in packets:
        for ep in packet.evidence_packets:
            number = reference_number_by_paper.get(ep.paper_id)
            if not number:
                continue
            replacements[f"[REF:{ep.paper_id}]"] = f"[{number}]"
            if ep.claim_id:
                replacements[f"[REF:{ep.paper_id}:{ep.claim_id}]"] = f"[{number}]"
    for marker in sorted(replacements, key=len, reverse=True):
        value = value.replace(marker, replacements[marker])
    return value, re.findall(r"\[REF:[^\]]+\]", value)


def _figure_markdown(draft: SectionDraft) -> str:
    lines: list[str] = []
    for index, placement in enumerate(draft.figure_placements, 1):
        path = str(placement.get("local_image_path") or "")
        if not path or not Path(path).exists():
            continue
        note = compact(placement.get("caption_note"), 500) or "Verified source visual."
        source = compact(placement.get("source_paper_id"), 180)
        lines.extend([
            f"\n**Figure asset {index}.** {note}",
            f"\n![Figure asset {index}]({Path(path).as_posix()})",
            f"\nSource: {source or 'canonical visual asset'}; publication permission must be checked.",
        ])
    return "\n".join(lines)


def _dedupe_pending_visual_assets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one richest auditable record for each requested visual."""
    best: dict[str, tuple[int, int, dict[str, Any]]] = {}
    order: list[str] = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        key = str(
            row.get("visual_plan_id")
            or row.get("visual_id")
            or row.get("chunk_id")
            or "|".join([
                str(row.get("section_id") or ""),
                str(row.get("visual_type") or row.get("asset_type") or ""),
                compact(
                    row.get("description")
                    or row.get("visual_argument_claim")
                    or row.get("caption_note"),
                    220,
                ),
            ])
        )
        if not key:
            key = f"anonymous:{index}"
        score = (
            10 * bool(str(row.get("local_image_path") or ""))
            + 5 * bool(row.get("visual_review") or row.get("review_result"))
            + 3 * bool(row.get("generation_status") or row.get("asset_status"))
            + len([value for value in row.values() if value not in (None, "", [], {})])
        )
        if key not in best:
            order.append(key)
        if key not in best or score > best[key][0]:
            best[key] = (score, index, row)
    return [best[key][2] for key in order]


def finalize_review(
    revision_bundle: dict[str, Any],
    global_bundle: dict[str, Any],
    peer_bundle: dict[str, Any],
    *,
    charter: dict[str, Any],
    kb_path: Path | str | None,
    real_llm: bool,
) -> dict[str, Any]:
    """Implement S20: gate, bibliography, visual placement, and final translation."""
    blueprint = revision_bundle.get("blueprint") or {}
    drafts = [draft_from_dict(row) for row in (revision_bundle.get("section_drafts") or [])]
    drafts = [remove_broken_visual_promises(draft) for draft in drafts]
    packets = [packet_from_dict(row) for row in (revision_bundle.get("material_packets") or [])]
    pending_visual_assets = _dedupe_pending_visual_assets([
        dict(plan)
        for packet in packets
        for plan in packet.visual_gap_plan
        if isinstance(plan, dict)
    ])
    sqlite_path = resolve_kb_sqlite(kb_path or revision_bundle.get("kb_sqlite"))
    cited_chunk_ids = list(dict.fromkeys(
        str(chunk_id)
        for draft in drafts
        for values in draft.citation_map.values()
        for chunk_id in values
        if chunk_id
    ))
    papers = _paper_rows_for_chunks(sqlite_path, cited_chunk_ids)
    number_by_paper = {
        str(row.get("paper_id") or ""): index for index, row in enumerate(papers, 1)
    }
    title = compact(
        charter.get("title")
        or charter.get("review_title")
        or blueprint.get("review_title")
        or "Scientific Literature Review",
        300,
    )
    title_by_id = {
        str(section.get("section_id") or ""): compact(
            section.get("title") or section.get("section_title"), 260
        )
        for section in (blueprint.get("sections") or [])
    }
    english_blocks: list[str] = [f"# {title}"]
    unresolved_internal_markers: list[str] = []
    for draft in drafts:
        text, unresolved = _replace_internal_markers(
            draft.english_text, packets, number_by_paper
        )
        unresolved_internal_markers.extend(unresolved)
        english_blocks.append(
            f"## {title_by_id.get(draft.section_id) or draft.section_id}\n\n{text.strip()}"
            + _figure_markdown(draft)
        )
    if papers:
        refs = ["## References"]
        for index, paper in enumerate(papers, 1):
            authors = str(paper.get("authors") or "").strip()
            prefix = f"{authors}. " if authors else ""
            doi = str(paper.get("doi") or "").strip()
            suffix = f" https://doi.org/{doi}" if doi else ""
            refs.append(
                f"{index}. {prefix}{paper.get('title') or 'Untitled source'}. "
                f"{paper.get('venue') or ''} ({paper.get('year') or 'n.d.'}).{suffix}"
            )
        english_blocks.append("\n".join(refs))
    english_review = "\n\n".join(block.strip() for block in english_blocks if block.strip()) + "\n"

    global_critical = int(global_bundle.get("critical_issue_count") or 0)
    peer_critical = int(peer_bundle.get("critical_issue_count") or 0)
    high_total = int(global_bundle.get("high_or_critical_issue_count") or 0) + int(
        peer_bundle.get("high_or_critical_issue_count") or 0
    )
    global_readiness = str(
        (global_bundle.get("judgment") or {}).get("formal_readiness") or ""
    )
    peer_recommendations = {
        str(row.get("recommendation") or "")
        for row in (peer_bundle.get("peer_reviews") or [])
        if isinstance(row, dict)
    }
    supervisor_summary = revision_bundle.get("supervisor_status_summary") or {}
    supervisor_critical_pending = int(supervisor_summary.get("critical_pending") or 0)
    supervisor_high_pending = int(supervisor_summary.get("high_pending") or 0)
    supervisor_process_errors = int(supervisor_summary.get("process_error_count") or 0)
    post_revision_audit = global_bundle.get("post_revision_citation_audit") or {}
    audited_section_count = len(post_revision_audit.get("citation_audits") or [])
    citation_ready_section_count = int(
        post_revision_audit.get("citation_ready_section_count") or 0
    )
    formal_ready_section_count = int(
        post_revision_audit.get("formal_ready_section_count") or 0
    )
    text_ready_section_count = int(
        post_revision_audit.get("text_ready_section_count") or 0
    )
    citation_gate_failed = bool(
        not post_revision_audit
        or audited_section_count != len(drafts)
        or citation_ready_section_count != len(drafts)
        or int(post_revision_audit.get("invalid_citation_count") or 0) > 0
        or int(post_revision_audit.get("uncited_load_bearing_claim_count") or 0) > 0
    )
    # Formal readiness is deliberately broader than citation readiness: a
    # section can have fully valid citations while still missing a required
    # figure, its word/paragraph contract, or an independent quality approval.
    # Keep these gates separate so the final report diagnoses the real defect.
    formal_contract_gate_failed = bool(
        not post_revision_audit
        or audited_section_count != len(drafts)
        or formal_ready_section_count != len(drafts)
    )
    text_contract_gate_failed = bool(
        not post_revision_audit
        or audited_section_count != len(drafts)
        or text_ready_section_count != len(drafts)
    )

    def non_visual_high_issues() -> int:
        rows: list[dict[str, Any]] = []
        judgment = global_bundle.get("judgment") or {}
        rows.extend(row for row in (judgment.get("issues") or []) if isinstance(row, dict))
        for review in (peer_bundle.get("peer_reviews") or []):
            if isinstance(review, dict):
                rows.extend(row for row in (review.get("issues") or []) if isinstance(row, dict))
        count = 0
        for issue in rows:
            severity = str(issue.get("severity") or "").lower()
            if severity not in {"high", "critical"}:
                continue
            issue_type = str(issue.get("issue_type") or issue.get("category") or "").lower()
            description = str(issue.get("description") or "").lower()
            if issue_type == "visual" or any(
                term in description for term in ("missing figure", "missing visual", "figure asset")
            ):
                continue
            count += 1
        return count

    non_visual_high_total = non_visual_high_issues()
    visuals_pending = bool(pending_visual_assets or formal_ready_section_count < text_ready_section_count)
    formal_status = (
        "mock_not_formal"
        if not real_llm
        else
        "blocked_by_critical_review_issues"
        if global_critical + peer_critical + supervisor_critical_pending > 0
        else "research_draft_needs_revision"
        if (
            non_visual_high_total > 0
            or supervisor_high_pending > 0
            or supervisor_process_errors > 0
            or citation_gate_failed
            or text_contract_gate_failed
            or unresolved_internal_markers
            or global_readiness not in {"ready", "ready_after_minor_revision"}
            or bool(peer_recommendations & {"major_revision", "reject"})
        )
        else "text_complete_visuals_pending"
        if visuals_pending
        else "formal_candidate"
    )

    title_zh = title
    title_by_id_zh = dict(title_by_id)
    heading_translation_failures: list[str] = []
    if real_llm:
        heading_payload = {
            "title": title,
            "section_titles": [
                {"section_id": section_id, "title": section_title}
                for section_id, section_title in title_by_id.items()
            ],
        }
        try:
            heading_result = call_qwen_chat(
                "FinalChineseHeadingTranslator",
                [
                    {
                        "role": "system",
                        "content": FINAL_HEADING_TRANSLATOR_PROMPT.read_text(encoding="utf-8"),
                    },
                    {"role": "user", "content": json.dumps(heading_payload, ensure_ascii=False)},
                ],
                model_tier="advanced_model",
                temperature=0,
                max_tokens=900,
                response_format={"type": "json_object"},
                force_mock=False,
                max_retries=1,
            )
            heading_parsed = _safe_json(str(heading_result.get("content") or ""))
        except Exception:
            heading_parsed = {}
        candidate_title = str(heading_parsed.get("title_zh") or "").strip()
        if candidate_title and re.search(r"[\u3400-\u9fff]", candidate_title):
            title_zh = candidate_title
        else:
            heading_translation_failures.append("TITLE")
        translated_rows = {
            str(row.get("section_id") or ""): str(row.get("title_zh") or "").strip()
            for row in (heading_parsed.get("section_titles_zh") or [])
            if isinstance(row, dict)
        }
        for section_id in title_by_id:
            translated_title = translated_rows.get(section_id, "")
            if translated_title and re.search(r"[\u3400-\u9fff]", translated_title):
                title_by_id_zh[section_id] = translated_title
            else:
                heading_translation_failures.append(section_id)

    translator = FinalTranslator(model_tier="advanced_model", real_llm=real_llm)
    premium_translator = FinalTranslator(model_tier="premium_model", real_llm=real_llm)
    translation_failures: list[str] = []
    chinese_blocks: list[str] = [f"# {title_zh}"]

    def translate_section(draft: SectionDraft) -> tuple[str, str, bool]:
        numeric_source_text, _ = _replace_internal_markers(
            draft.english_text, packets, number_by_paper
        )
        source_citations = set(re.findall(r"\[\d+(?:\s*,\s*\d+)*\]", numeric_source_text))
        translated = copy.deepcopy(draft)
        translated.english_text = numeric_source_text
        if real_llm:
            try:
                translator.translate(translated)
            except Exception:
                translated.chinese_text = ""
        else:
            translated.chinese_text = "[Mock mode: final Chinese translation was not generated.]"
        target_citations = set(re.findall(r"\[\d+(?:\s*,\s*\d+)*\]", translated.chinese_text))
        def translation_failed() -> bool:
            current_citations = set(re.findall(
                r"\[\d+(?:\s*,\s*\d+)*\]", translated.chinese_text
            ))
            return bool(real_llm and (
                not translated.chinese_text.strip()
                or len(translated.chinese_text.strip())
                < max(30, int(len(draft.english_text) * 0.20))
                or source_citations - current_citations
            ))

        failed = translation_failed()
        if failed and real_llm:
            # A section-level premium retry is cheaper and safer than
            # retranslating the complete manuscript or accepting a truncated
            # chapter.  The same citation-completeness gate is applied again.
            translated.chinese_text = ""
            try:
                premium_translator.translate(translated)
            except Exception:
                translated.chinese_text = ""
            failed = translation_failed()
        return draft.section_id, translated.chinese_text.strip(), failed

    # Sections are independent after numeric citations have been frozen.
    # Bounded parallel translation prevents one slow response from blocking an
    # otherwise complete manuscript, while results are assembled in canonical
    # section order and each section retains its own citation-completeness gate.
    translated_by_id: dict[str, tuple[str, bool]] = {}
    if real_llm:
        with ThreadPoolExecutor(max_workers=min(3, len(drafts))) as pool:
            futures = [pool.submit(translate_section, draft) for draft in drafts]
            for future in as_completed(futures):
                try:
                    section_id, translated_text, failed = future.result()
                    translated_by_id[section_id] = (translated_text, failed)
                except Exception:
                    continue
    else:
        for draft in drafts:
            section_id, translated_text, failed = translate_section(draft)
            translated_by_id[section_id] = (translated_text, failed)

    for draft in drafts:
        translated_text, failed = translated_by_id.get(draft.section_id, ("", True))
        if failed:
            translation_failures.append(draft.section_id)
        chinese_blocks.append(
            f"## {title_by_id_zh.get(draft.section_id) or draft.section_id}\n\n"
            f"{translated_text}"
        )
    if papers:
        # Bibliographic metadata remains in its source language by design.
        chinese_blocks.append(english_blocks[-1].replace("## References", "## 参考文献", 1))
    chinese_review = "\n\n".join(block.strip() for block in chinese_blocks if block.strip()) + "\n"
    if (
        translation_failures or heading_translation_failures
    ) and formal_status == "formal_candidate":
        formal_status = "translation_needs_human_review"

    return {
        "schema_version": "full_review.final_outputs.v1",
        "title": title,
        "english_review": english_review,
        "chinese_review": chinese_review,
        "bibliography": papers,
        "pending_visual_assets": pending_visual_assets,
        "formal_status": formal_status,
        "unresolved_internal_reference_markers": sorted(set(unresolved_internal_markers)),
        "translation_failure_section_ids": translation_failures,
        "heading_translation_failures": heading_translation_failures,
        "quality_summary": {
            "english_word_count": len(re.findall(r"\b[A-Za-z][A-Za-z'-]*\b", english_review)),
            "reference_count": len(papers),
            "figure_asset_count": sum(
                bool(str(item.get("local_image_path") or ""))
                and Path(str(item.get("local_image_path") or "")).exists()
                for draft in drafts for item in draft.figure_placements
            ),
            "pending_visual_asset_count": len(pending_visual_assets),
            "global_critical_issues": global_critical,
            "peer_critical_issues": peer_critical,
            "high_or_critical_issues": high_total,
            "supervisor_high_pending": supervisor_high_pending,
            "supervisor_critical_pending": supervisor_critical_pending,
            "supervisor_process_errors": supervisor_process_errors,
            "supervisor_review_complete": bool(
                supervisor_summary.get("review_complete", supervisor_process_errors == 0)
            ),
            "citation_gate_failed": citation_gate_failed,
            "formal_contract_gate_failed": formal_contract_gate_failed,
            "text_contract_gate_failed": text_contract_gate_failed,
            "non_visual_high_or_critical_issues": non_visual_high_total,
            "visuals_pending": visuals_pending,
            "citation_ready_sections": citation_ready_section_count,
            "text_ready_sections": text_ready_section_count,
            "formal_ready_sections": formal_ready_section_count,
            # Backward-compatible alias retained for existing consumers.  New
            # code should use ``formal_ready_sections``.
            "citation_formal_ready_sections": formal_ready_section_count,
            "citation_audited_sections": audited_section_count,
        },
        "delivery_policy": (
            "The pipeline always delivers an auditable manuscript, but only labels it a formal "
            "candidate when citation identifiers resolve and independent reviews report no "
            "high or critical blockers."
        ),
    }
