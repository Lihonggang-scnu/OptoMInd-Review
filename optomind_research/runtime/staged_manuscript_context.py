"""Deterministic stage-context preparation for staged full-manuscript calls.

This module converts ``UNIFIED_MANUSCRIPT_HANDOFF.json`` plus a global
commander work order into two context artifacts:

- ``STAGED_GLOBAL_INPUTS.json``: shared normalized content (per-section text,
  stable block IDs/hashes, theses, terminology, review summaries, citation
  inventory, local background candidates, commander structure);
- ``STAGED_STAGE_INPUTS.json``: stage-specific inputs for conclusion,
  introduction, abstract, whole_manuscript_review, and
  bounded_patch_proposals.

Every project-relative path in the handoff is resolved from ``project_root``
and its sha256 digest is validated against the handoff record before use.
The fingerprint depends only on content (never absolute paths), and the
outputs carry project-relative source provenance only.  No model is called
and no evidence is promoted: core packets stay ``core_evidence`` and
explanatory ledger entries stay ``background_explanation_only``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "optomind.staged_manuscript_context.v1"
GLOBAL_INPUTS_JSON = "STAGED_GLOBAL_INPUTS.json"
STAGE_INPUTS_JSON = "STAGED_STAGE_INPUTS.json"
STAGE_KEYS = (
    "conclusion",
    "introduction",
    "abstract",
    "whole_manuscript_review",
    "bounded_patch_proposals",
    "editorial_revision",
)
REVIEWER_SCOPE = ("continuity", "clarity", "reader_flow", "logic", "overlap")


class StagedContextError(RuntimeError):
    """Raised when stage context cannot be prepared safely."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise StagedContextError(f"{label}: cannot read {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise StagedContextError(
            f"{label}: expected a JSON object: {path}"
        )
    return dict(payload)


def _verify_and_read(
    record: Mapping[str, Any],
    *,
    project_root: Path,
    label: str,
) -> Path:
    raw_path = str(record.get("path") or "")
    if not raw_path:
        raise StagedContextError(f"{label}: missing path record")
    path = Path(raw_path)
    if not path.is_absolute():
        path = project_root / path
    if not path.is_file():
        raise StagedContextError(f"{label}: missing file {path}")
    digest = _sha256_bytes(path.read_bytes())
    expected = str(record.get("sha256") or "")
    if expected and digest != expected:
        raise StagedContextError(
            f"{label}: digest mismatch for {path} "
            f"(expected {expected}, got {digest})"
        )
    return path


def _project_relative(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _block_records(blocks_data: Mapping[str, Any], section_id: str) -> list[dict[str, Any]]:
    raw_blocks = [
        raw
        for raw in blocks_data.get("blocks") or []
        if isinstance(raw, Mapping)
    ]
    if not raw_blocks:
        return []
    has_any_index = any("block_index" in block for block in raw_blocks)
    if has_any_index:
        parsed_indices: list[int | None] = []
        for block in raw_blocks:
            try:
                parsed_indices.append(int(block.get("block_index")))
            except (TypeError, ValueError):
                parsed_indices.append(None)
        if (
            any(index is None or index <= 0 for index in parsed_indices)
            or len(set(parsed_indices)) != len(parsed_indices)
        ):
            raise StagedContextError(
                f"{section_id}: duplicate/nonpositive/missing block_index "
                "in EXPLANATION_BLOCKS"
            )
        indices = [int(index) for index in parsed_indices]
    else:
        indices = list(range(1, len(raw_blocks) + 1))
    records: list[dict[str, Any]] = []
    for local_index, (raw, block_index) in enumerate(
        zip(raw_blocks, indices), start=1
    ):
        block_id = f"{section_id}-B{block_index:03d}"
        prose = str(raw.get("prose") or "")
        records.append(
            {
                "block_id": block_id,
                "block_index": block_index,
                "local_index": local_index,
                "title": str(raw.get("title") or ""),
                "prose": prose,
                "sha256": _sha256_bytes(prose.encode("utf-8")),
                "goal": str(raw.get("goal") or ""),
                "markers": [
                    str(value)
                    for value in raw.get("markers") or []
                    if str(value)
                ],
                "explanatory_markers": [
                    str(value)
                    for value in raw.get("explanatory_markers") or []
                    if str(value)
                ],
                "claim_handles": [
                    str(value)
                    for value in raw.get("claim_handles") or []
                    if str(value)
                ],
                "evidence_handles": [
                    str(value)
                    for value in raw.get("evidence_handles") or []
                    if str(value)
                ],
            }
        )
    return records


def _review_summary(review_path: Path | None) -> dict[str, Any]:
    if review_path is None:
        return {
            "available": False,
            "status": "missing_fail_open",
            "advisory_count": 0,
            "blocking_count": 0,
            "comments": [],
        }
    review = _read_json(review_path, "block scientific review")
    comments = [
        {
            "block_index": int(comment.get("block_index") or 0),
            "flag_type": str(comment.get("flag_type") or ""),
            "issue": str(comment.get("issue") or ""),
            "blocking": bool(comment.get("blocking")),
        }
        for comment in review.get("comments") or []
        if isinstance(comment, Mapping)
    ]
    return {
        "available": True,
        "status": "present",
        "advisory_count": sum(1 for comment in comments if not comment["blocking"]),
        "blocking_count": sum(1 for comment in comments if comment["blocking"]),
        "comments": comments,
    }


def _ledger_entries(ledger: Mapping[str, Any], section_id: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for raw in ledger.get("records") or []:
        if not isinstance(raw, Mapping):
            continue
        metadata = raw.get("metadata") or {}
        metadata = metadata if isinstance(metadata, Mapping) else {}
        marker_identity = str(
            raw.get("marker_id")
            or raw.get("marker")
            or raw.get("handle")
            or metadata.get("paper_id")
            or metadata.get("doi")
            or ""
        ).strip()
        if not marker_identity:
            continue
        metadata = metadata if isinstance(metadata, Mapping) else {}
        helpfulness_score = raw.get("helpfulness_score")
        if not isinstance(helpfulness_score, (int, float)):
            helpfulness_score = None
        selection_score = raw.get("selection_score")
        if not isinstance(selection_score, (int, float)):
            selection_score = None
        authors_raw = metadata.get("authors") or metadata.get("author") or []
        if isinstance(authors_raw, str):
            authors_raw = [authors_raw]
        entries.append(
            {
                "citation_id": marker_identity,
                "title": str(metadata.get("title") or ""),
                "paper_id": str(metadata.get("paper_id") or ""),
                "doi": str(metadata.get("doi") or ""),
                "abstract": str(
                    metadata.get("abstract")
                    or metadata.get("abstract_text")
                    or metadata.get("summary")
                    or ""
                ),
                "year": str(
                    metadata.get("year")
                    or metadata.get("publication_year")
                    or ""
                ).strip(),
                "venue": str(
                    metadata.get("venue")
                    or metadata.get("journal")
                    or metadata.get("source_title")
                    or metadata.get("conference")
                    or ""
                ),
                "authors": [
                    str(author)
                    for author in authors_raw
                    if str(author).strip()
                ],
                "trust_type": "background_explanation_only",
                "helpfulness_score": (
                    float(helpfulness_score)
                    if helpfulness_score is not None
                    else None
                ),
                "selection_score": (
                    float(selection_score)
                    if selection_score is not None
                    else None
                ),
                "overlaps_core_reference": bool(
                    raw.get("overlaps_core_reference")
                ),
                "permission": str(raw.get("permission") or ""),
                "trust": str(raw.get("permission") or ""),
                "provenance": {"section_id": section_id},
            }
        )
    return entries


def _core_entries(packet: Mapping[str, Any], section_id: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for entry in packet.get("evidence_packets") or []:
        if not isinstance(entry, Mapping):
            continue
        paper_id = str(entry.get("paper_id") or "")
        if not paper_id:
            continue
        entries.append(
            {
                "citation_id": paper_id,
                "title": str(entry.get("source_title") or ""),
                "trust_type": "core_evidence",
                "provenance": {"section_id": section_id},
            }
        )
    for source in (packet.get("literature_coverage") or {}).get("sources") or []:
        if not isinstance(source, Mapping):
            continue
        paper_id = str(source.get("paper_id") or "")
        if not paper_id:
            continue
        entries.append(
            {
                "citation_id": paper_id,
                "title": str(source.get("title") or ""),
                "trust_type": "core_evidence",
                "provenance": {"section_id": section_id},
            }
        )
    return entries


def _normalize_section(
    envelope: Mapping[str, Any],
    *,
    project_root: Path,
) -> dict[str, Any]:
    section_id = str(envelope.get("section_id") or "")
    content_status = str(envelope.get("content_status") or "enhanced")
    if content_status == "raw_fallback":
        draft_path = _verify_and_read(
            envelope.get("enhanced_chapter") or {},
            project_root=project_root,
            label=f"{section_id} raw fallback draft",
        )
        packet_path = _verify_and_read(
            envelope.get("authoritative_input_packet") or {},
            project_root=project_root,
            label=f"{section_id} raw fallback input packet",
        )
        full_text = draft_path.read_text(encoding="utf-8")
        packet = _read_json(packet_path, f"{section_id} input packet")
        section_contract = packet.get("section_contract") or {}
        section_contract = (
            section_contract if isinstance(section_contract, Mapping) else {}
        )
        manuscript_context = packet.get("manuscript_context") or {}
        manuscript_context = (
            manuscript_context
            if isinstance(manuscript_context, Mapping)
            else {}
        )
        research_context = manuscript_context.get("research_context") or {}
        research_context = (
            research_context if isinstance(research_context, Mapping) else {}
        )
        argument_role = section_contract.get("argument_role")
        narrative_strategy = (
            str(argument_role.get("statement") or "")
            if isinstance(argument_role, Mapping)
            else str(argument_role or "")
        )
        blocks = _block_records(
            {
                "blocks": [
                    {
                        "block_index": 1,
                        "title": str(envelope.get("section_title") or ""),
                        "prose": full_text,
                        "goal": "raw_fallback",
                    }
                ]
            },
            section_id,
        )
        return {
            "section_id": section_id,
            "section_title": str(envelope.get("section_title") or ""),
            "word_count": int(envelope.get("word_count") or 0),
            "chapter_status": content_status,
            "full_text": full_text,
            "full_text_sha256": _sha256_bytes(full_text.encode("utf-8")),
            "blocks": blocks,
            "chapter_thesis": str(
                section_contract.get("central_thesis")
                or section_contract.get("section_purpose")
                or ""
            ),
            "reader_takeaway": "",
            "argument_sequence": [],
            "terminology": [],
            "review_summary": {
                "available": False,
                "status": "raw_fallback",
                "advisory_count": 1,
                "blocking_count": 0,
                "comments": [],
            },
            "core_trust_boundary": str(
                (envelope.get("authoritative_input_packet") or {}).get("role")
                or "core_evidence"
            ),
            "explanatory_trust_boundary": "background_explanation_only",
            "user_question": str(research_context.get("user_question") or ""),
            "problem_understanding": str(
                research_context.get("problem_understanding") or ""
            ),
            "central_thesis": str(
                section_contract.get("central_thesis")
                or section_contract.get("section_purpose")
                or ""
            ),
            "narrative_strategy": narrative_strategy,
            "global_review_thesis": str(
                manuscript_context.get("global_review_thesis")
                or manuscript_context.get("review_thesis")
                or ""
            ),
            "global_narrative_strategy": str(
                manuscript_context.get("global_narrative_strategy")
                or manuscript_context.get("narrative_strategy")
                or ""
            ),
            "enhancement_status": content_status,
            "provenance": {
                "raw_fallback_draft": _project_relative(
                    draft_path, project_root
                ),
                "authoritative_input_packet": _project_relative(
                    packet_path, project_root
                ),
                "fallback_warning": str(
                    (envelope.get("provenance") or {}).get(
                        "fallback_warning"
                    )
                    or ""
                ),
            },
        }
    enhanced_path = _verify_and_read(
        envelope.get("enhanced_chapter") or {},
        project_root=project_root,
        label=f"{section_id} enhanced chapter",
    )
    plan_path = _verify_and_read(
        envelope.get("argument_plan") or {},
        project_root=project_root,
        label=f"{section_id} argument plan",
    )
    blocks_path = _verify_and_read(
        envelope.get("explanation_blocks") or {},
        project_root=project_root,
        label=f"{section_id} explanation blocks",
    )
    ledger_path = _verify_and_read(
        envelope.get("explanatory_citation_ledger") or {},
        project_root=project_root,
        label=f"{section_id} explanatory ledger",
    )
    report_path = _verify_and_read(
        envelope.get("enhancement_report") or {},
        project_root=project_root,
        label=f"{section_id} enhancement report",
    )
    packet_path = _verify_and_read(
        envelope.get("authoritative_input_packet") or {},
        project_root=project_root,
        label=f"{section_id} input packet",
    )
    full_text = enhanced_path.read_text(encoding="utf-8")
    plan = _read_json(plan_path, f"{section_id} argument plan")
    blocks_data = _read_json(blocks_path, f"{section_id} explanation blocks")
    ledger = _read_json(ledger_path, f"{section_id} explanatory ledger")
    report = _read_json(report_path, f"{section_id} enhancement report")
    packet = _read_json(packet_path, f"{section_id} input packet")

    plan_payload = plan.get("plan") if isinstance(plan.get("plan"), Mapping) else {}
    review_record = envelope.get("reviewer_notes")
    review_path = (
        _verify_and_read(
            review_record,
            project_root=project_root,
            label=f"{section_id} block scientific review",
        )
        if isinstance(review_record, Mapping) and review_record.get("path")
        else None
    )
    research_context = packet.get("manuscript_context") or {}
    manuscript_context = (
        research_context if isinstance(research_context, Mapping) else {}
    )
    research_context = (
        manuscript_context.get("research_context")
        if isinstance(manuscript_context, Mapping)
        else {}
    )
    research_context = (
        research_context if isinstance(research_context, Mapping) else {}
    )
    section_contract = packet.get("section_contract") or {}
    section_contract = (
        section_contract if isinstance(section_contract, Mapping) else {}
    )
    argument_role = section_contract.get("argument_role")
    if isinstance(argument_role, Mapping):
        narrative_strategy = str(argument_role.get("statement") or "")
    else:
        narrative_strategy = str(argument_role or "")
    global_review_thesis = str(
        manuscript_context.get("global_review_thesis")
        or manuscript_context.get("review_thesis")
        or ""
    )
    global_narrative_strategy = str(
        manuscript_context.get("global_narrative_strategy")
        or manuscript_context.get("narrative_strategy")
        or ""
    )
    return {
        "section_id": section_id,
        "section_title": str(envelope.get("section_title") or ""),
        "word_count": int(envelope.get("word_count") or 0),
        "chapter_status": str(envelope.get("chapter_status") or "unknown"),
        "full_text": full_text,
        "full_text_sha256": _sha256_bytes(full_text.encode("utf-8")),
        "blocks": _block_records(blocks_data, section_id),
        "chapter_thesis": str(plan_payload.get("chapter_thesis") or ""),
        "reader_takeaway": str(plan_payload.get("reader_takeaway") or ""),
        "argument_sequence": plan_payload.get("argument_sequence") or [],
        "terminology": plan_payload.get("terminology_rows") or [],
        "review_summary": _review_summary(review_path),
        "core_trust_boundary": str(
            (envelope.get("authoritative_input_packet") or {}).get("role")
            or "core_evidence"
        ),
        "explanatory_trust_boundary": str(
            (envelope.get("explanatory_citation_ledger") or {}).get(
                "trust_boundary"
            )
            or "background_explanation_only"
        ),
        "user_question": str(
            research_context.get("user_question") or ""
        ),
        "problem_understanding": str(
            research_context.get("problem_understanding") or ""
        ),
        "central_thesis": str(
            section_contract.get("central_thesis")
            or section_contract.get("section_purpose")
            or ""
        ),
        "narrative_strategy": narrative_strategy,
        "global_review_thesis": global_review_thesis,
        "global_narrative_strategy": global_narrative_strategy,
        "enhancement_status": str(report.get("status") or "unknown"),
        "provenance": {
            "enhanced_chapter": _project_relative(enhanced_path, project_root),
            "argument_plan": _project_relative(plan_path, project_root),
            "explanation_blocks": _project_relative(
                blocks_path, project_root
            ),
            "explanatory_citation_ledger": _project_relative(
                ledger_path, project_root
            ),
            "enhancement_report": _project_relative(report_path, project_root),
            "authoritative_input_packet": _project_relative(
                packet_path, project_root
            ),
        },
    }


def _dedupe_background_candidates(
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    def rank_key(entry: Mapping[str, Any]) -> tuple[float, float, str]:
        helpfulness = entry.get("helpfulness_score")
        helpfulness = (
            float(helpfulness) if helpfulness is not None else float("-inf")
        )
        selection = float(entry.get("selection_score") or 0.0)
        return (
            -helpfulness,
            -selection,
            str(entry.get("citation_id") or ""),
        )

    by_identity: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        identity = str(candidate.get("citation_id") or "")
        if not identity:
            continue
        existing = by_identity.get(identity)
        if existing is None or rank_key(candidate) < rank_key(existing):
            by_identity[identity] = dict(candidate)
    return sorted(by_identity.values(), key=rank_key)


def _commander_structure(work_order: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "manuscript_diagnosis": str(work_order.get("manuscript_diagnosis") or ""),
        "proposed_section_order": work_order.get("proposed_section_order") or [],
        "section_decisions": work_order.get("section_decisions") or [],
        "structure_gaps": work_order.get("structure_gaps") or [],
        "missing_axes": work_order.get("missing_axes") or [],
        "cross_section_conflicts": work_order.get(
            "cross_section_conflicts"
        )
        or work_order.get("repeated_paper_role_audit")
        or [],
        "visual_work_orders": work_order.get("visual_work_orders") or [],
        "reviewer_carryover": work_order.get("retained_advisory_issues") or [],
    }


def _unique_nonempty(values: Sequence[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if isinstance(value, Mapping):
            text = str(
                value.get("statement")
                or value.get("description")
                or value.get("issue")
                or value.get("title")
                or value.get("axis")
                or ""
            ).strip()
        else:
            text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _build_presentation_ir(
    *,
    user_question: str,
    global_review_thesis: str,
    global_narrative_strategy: str,
    sections: Sequence[Mapping[str, Any]],
    commander: Mapping[str, Any],
    inventory: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the deterministic article-level rhetorical context.

    This is deliberately an extraction layer, not a new scientific judge:
    it only repackages already admitted section and commander fields for the
    staged front-matter authors. Missing or weak fields remain empty so the
    existing fail-open prompt path still works.
    """

    terminology: dict[str, str] = {}
    for section in sections:
        for row in section.get("terminology") or []:
            if not isinstance(row, Mapping):
                continue
            term = str(row.get("term") or row.get("canonical_term") or "").strip()
            expansion = str(
                row.get("expansion")
                or row.get("definition")
                or row.get("preferred_form")
                or ""
            ).strip()
            if term and expansion and term not in terminology:
                terminology[term] = expansion

    synthesis_claims = _unique_nonempty(
        [
            global_review_thesis,
            *[
                str(section.get("chapter_thesis") or "")
                for section in sections
            ],
        ]
    )
    safe_citations = _unique_nonempty(
        [
            entry.get("citation_id")
            for entry in inventory
            if str(entry.get("trust_type") or "") == "core_evidence"
        ]
    )[:24]
    return {
        "schema_version": "optomind.literature_review_presentation_ir.v1",
        "central_topic": user_question,
        "review_subtype": "critical literature review",
        "organizing_principle": global_narrative_strategy,
        "synthesis_claims": synthesis_claims,
        "consensus": [],
        "controversies": _unique_nonempty(
            commander.get("cross_section_conflicts") or []
        ),
        "bottlenecks": _unique_nonempty(
            commander.get("structure_gaps") or []
        ),
        "emerging_directions": _unique_nonempty(
            commander.get("missing_axes") or []
        ),
        "safe_citations": safe_citations,
        "forbidden_claims": [
            "Do not introduce unsupported evidence or alter bound claim/evidence relationships."
        ],
        "terminology_registry": terminology,
        "provenance": "deterministic_staged_manuscript_context",
    }


def build_staged_manuscript_context(
    *,
    project_root: str | Path,
    handoff_path: str | Path,
    commander_work_order_path: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Build global and stage-specific inputs from handoff + work order."""

    project_root = Path(project_root).resolve()
    handoff = _read_json(Path(handoff_path), "unified handoff")
    work_order = _read_json(Path(commander_work_order_path), "commander work order")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    sections: list[dict[str, Any]] = []
    citation_entries: list[dict[str, Any]] = []
    background_candidates: list[dict[str, Any]] = []
    for section_id in handoff.get("section_order") or []:
        envelope = handoff.get("sections") or {}
        section_envelope = envelope.get(section_id)
        if not isinstance(section_envelope, Mapping):
            raise StagedContextError(
                f"handoff missing section envelope: {section_id}"
            )
        normalized = _normalize_section(
            section_envelope, project_root=project_root
        )
        sections.append(normalized)
        citation_entries.extend(
            _core_entries(
                _read_json(
                    _verify_and_read(
                        section_envelope.get("authoritative_input_packet") or {},
                        project_root=project_root,
                        label=f"{section_id} input packet",
                    ),
                    f"{section_id} input packet",
                ),
                section_id,
            )
        )
        ledger_record = section_envelope.get("explanatory_citation_ledger")
        if isinstance(ledger_record, Mapping) and ledger_record.get("path"):
            ledger_data = _read_json(
                _verify_and_read(
                    ledger_record,
                    project_root=project_root,
                    label=f"{section_id} explanatory ledger",
                ),
                f"{section_id} explanatory ledger",
            )
            ledger_entries = _ledger_entries(ledger_data, section_id)
            citation_entries.extend(ledger_entries)
            background_candidates.extend(ledger_entries)

    user_question = ""
    global_review_thesis = ""
    global_narrative_strategy = ""
    for section in sections:
        if not user_question and section["user_question"]:
            user_question = section["user_question"]
        if not global_review_thesis and section["global_review_thesis"]:
            global_review_thesis = section["global_review_thesis"]
        if not global_narrative_strategy and section[
            "global_narrative_strategy"
        ]:
            global_narrative_strategy = section["global_narrative_strategy"]

    def unique(values: Sequence[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            if value and value not in seen:
                seen.add(value)
                result.append(value)
        return result

    user_questions = unique([section["user_question"] for section in sections])
    if user_questions:
        user_question = user_questions[0]
    background_candidates = _dedupe_background_candidates(background_candidates)
    commander = _commander_structure(work_order)
    inventory = sorted(
        citation_entries,
        key=lambda item: (
            str(item.get("citation_id") or ""),
            str(item.get("trust_type") or ""),
        ),
    )
    inventory = _dedupe_inventory(inventory)
    presentation_ir = _build_presentation_ir(
        user_question=user_question,
        global_review_thesis=global_review_thesis,
        global_narrative_strategy=global_narrative_strategy,
        sections=sections,
        commander=commander,
        inventory=inventory,
    )

    aggregate_counts = {
        "section_count": len(sections),
        "block_count": sum(len(section["blocks"]) for section in sections),
        "citation_inventory_count": len(inventory),
        "local_background_candidate_count": len(background_candidates),
        "total_word_count": sum(
            section["word_count"] for section in sections
        ),
    }
    global_fingerprint = fingerprint(
        {
            "schema_version": SCHEMA_VERSION,
            "handoff_fingerprint": handoff.get("input_fingerprint") or "",
            "work_order_fingerprint": work_order.get("fingerprint") or "",
            "sections": [
                {
                    "section_id": section["section_id"],
                    "title": section["section_title"],
                    "full_text_sha256": section["full_text_sha256"],
                    "blocks": [
                        {
                            "block_id": block["block_id"],
                            "sha256": block["sha256"],
                        }
                        for block in section["blocks"]
                    ],
                    "chapter_thesis": section["chapter_thesis"],
                    "reader_takeaway": section["reader_takeaway"],
                    "terminology": section["terminology"],
                }
                for section in sections
            ],
            "commander": commander,
            "citation_inventory": inventory,
            "background_candidates": background_candidates,
            "aggregate_counts": aggregate_counts,
            "presentation_ir": presentation_ir,
        }
    )

    global_inputs = {
        "schema_version": SCHEMA_VERSION,
        "input_fingerprint": global_fingerprint,
        "handoff_input_fingerprint": handoff.get("input_fingerprint") or "",
        "user_question": user_question,
        "global_review_thesis": global_review_thesis,
        "global_narrative_strategy": global_narrative_strategy,
        "commander_structure": commander,
        "sections": sections,
        "citation_inventory": inventory,
        "local_background_candidates": background_candidates,
        "presentation_ir": presentation_ir,
        "aggregate_counts": aggregate_counts,
        "source_provenance": {
            "handoff": _project_relative(
                Path(handoff_path).resolve(), project_root
            ),
            "commander_work_order": _project_relative(
                Path(commander_work_order_path).resolve(), project_root
            ),
            "sections": {
                section["section_id"]: section["provenance"]
                for section in sections
            },
        },
    }

    compact_sections = [
        {
            "section_id": section["section_id"],
            "section_title": section["section_title"],
            "chapter_thesis": section["chapter_thesis"],
            "reader_takeaway": section["reader_takeaway"],
        }
        for section in sections
    ]
    section_summaries = [
        {
            "section_id": section["section_id"],
            "section_title": section["section_title"],
            "word_count": section["word_count"],
            "chapter_status": section["chapter_status"],
            "chapter_thesis": section["chapter_thesis"],
            "reader_takeaway": section["reader_takeaway"],
            "block_ids": [block["block_id"] for block in section["blocks"]],
        }
        for section in sections
    ]
    problem_understanding = ""
    for section in sections:
        if not problem_understanding and section["problem_understanding"]:
            problem_understanding = section["problem_understanding"]
    stage_inputs = {
        "conclusion": {
            "sections": [
                {
                    "section_id": section["section_id"],
                    "section_title": section["section_title"],
                    "full_text": section["full_text"],
                    "chapter_thesis": section["chapter_thesis"],
                    "reader_takeaway": section["reader_takeaway"],
                }
                for section in sections
            ],
            "commander_structure": commander,
            "user_question": user_question,
            "problem_understanding": problem_understanding,
            "global_review_thesis": global_review_thesis,
            "global_narrative_strategy": global_narrative_strategy,
            "soft_word_target": {"min": 500, "max": 900},
            "constraints": ["no_new_evidence", "no_new_topics"],
        },
        "introduction": {
            "structure": {
                "section_order": [section["section_id"] for section in sections],
                "sections": compact_sections,
            },
            "user_question": user_question,
            "problem_understanding": problem_understanding,
            "global_review_thesis": global_review_thesis,
            "global_narrative_strategy": global_narrative_strategy,
            "commander_structure": commander,
            "conclusion_source": "previous_artifacts.conclusion",
            "local_background_candidates": background_candidates,
            "soft_word_target": {"min": 800, "max": 1300},
            "retrieval_proposals_allowed": True,
        },
        "abstract": {
            "article_identity": {
                "user_question": user_question,
                "global_review_thesis": global_review_thesis,
                "global_narrative_strategy": global_narrative_strategy,
                "section_order": [section["section_id"] for section in sections],
            },
            "section_summaries": section_summaries,
            "soft_word_target": {"min": 220, "max": 300},
            "front_back_sources": (
                "previous_artifacts.conclusion/introduction"
            ),
        },
        "whole_manuscript_review": {
            "sections": [
                {
                    "section_id": section["section_id"],
                    "section_title": section["section_title"],
                    "full_text": section["full_text"],
                    "blocks": section["blocks"],
                }
                for section in sections
            ],
            "commander_structure": commander,
            "user_question": user_question,
            "global_review_thesis": global_review_thesis,
            "global_narrative_strategy": global_narrative_strategy,
            "reviewer_scope": list(REVIEWER_SCOPE),
            "front_back_sources": (
                "previous_artifacts.conclusion/introduction/abstract"
            ),
        },
        "bounded_patch_proposals": {
            "sections": [
                {
                    "section_id": section["section_id"],
                    "section_title": section["section_title"],
                    "blocks": section["blocks"],
                }
                for section in sections
            ],
            "commander_decisions": commander,
            "reviewer_sources": "previous_artifacts.whole_manuscript_review",
            "preserve_claim_evidence": True,
        },
        "editorial_revision": {
            "sections": [
                {
                    "section_id": section["section_id"],
                    "section_title": section["section_title"],
                    "full_text": section["full_text"],
                    "chapter_thesis": section["chapter_thesis"],
                    "reader_takeaway": section["reader_takeaway"],
                    "terminology": section["terminology"],
                    "blocks": section["blocks"],
                }
                for section in sections
            ],
            "commander_structure": commander,
            "section_order": [
                section["section_id"] for section in sections
            ],
            "reviewer_sources": "previous_artifacts.whole_manuscript_review",
            "patch_sources": "previous_artifacts.bounded_patch_proposals",
            "front_back_sources": (
                "previous_artifacts.conclusion/introduction/abstract"
            ),
            "preserve_claim_evidence": True,
        },
    }
    stage_output = {
        "schema_version": SCHEMA_VERSION,
        "input_fingerprint": global_fingerprint,
        "stage_keys": list(STAGE_KEYS),
        "stages": stage_inputs,
    }

    global_path = output_dir / GLOBAL_INPUTS_JSON
    stage_path = output_dir / STAGE_INPUTS_JSON
    global_path.write_text(
        json.dumps(global_inputs, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    stage_path.write_text(
        json.dumps(stage_output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "input_fingerprint": global_fingerprint,
        "stage_keys": list(STAGE_KEYS),
        "aggregate_counts": aggregate_counts,
        "output_paths": {
            "global_inputs": str(global_path),
            "stage_inputs": str(stage_path),
        },
    }


def _dedupe_inventory(
    entries: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for entry in entries:
        key = (
            str(entry.get("citation_id") or ""),
            str(entry.get("trust_type") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(entry))
    return result


__all__ = [
    "SCHEMA_VERSION",
    "GLOBAL_INPUTS_JSON",
    "STAGE_INPUTS_JSON",
    "STAGE_KEYS",
    "REVIEWER_SCOPE",
    "StagedContextError",
    "canonical_json",
    "fingerprint",
    "build_staged_manuscript_context",
]
