"""Targeted revision worker — applies a RevisionPlan to specific sections.

Inline patches (no rerun): deterministic string edits to SECTION_DRAFT_EN.md.
Full reruns: call ResearchWorker with updated SectionAuthoringContext.
Every inline patch is wrapped in a RevisionTransaction: snapshot → apply →
validate → commit or rollback. Results written to REVISION_HISTORY.json.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_MIN_SECTION_WORDS = 50
_MIN_RETENTION_RATIO = 0.60  # after edit, section must keep ≥60% of original word count


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _word_count(text: str) -> int:
    return len(text.split())


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _extract_ref_mentions(text: str) -> List[str]:
    """Return all [REF:...] citation markers found in text."""
    return re.findall(r'\[REF:[^\]]+\]', text)


def _strip_repeated_leading_title(text: str, title: str) -> str:
    """Remove only a draft's leading heading when it repeats the wrapper title."""
    lines = str(text or "").lstrip("\ufeff \t\r\n").splitlines()
    if not lines:
        return ""
    first = re.sub(r"^#{1,6}\s+", "", lines[0]).strip()
    canonical = re.sub(r"\s+", " ", str(title or "")).strip().casefold()
    if re.sub(r"\s+", " ", first).casefold() == canonical:
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def _demote_embedded_section_headings(text: str) -> str:
    """Reserve H2 for planned article sections.

    Section authors may use H1/H2 subheadings in their local draft.  Once the
    draft is wrapped by the article-level H2 title, those headings must become
    H3 so quality gates and readers see the real blueprint structure rather
    than an inflated section count.
    """

    return re.sub(
        r"(?m)^(#{1,2})(\s+)",
        lambda match: "###" + match.group(2),
        str(text or ""),
    ).strip()


def _extract_load_bearing_claim_ids(section_meta: Dict[str, Any]) -> List[str]:
    """Return real claim_ids for load-bearing claims.

    Priority: SECTION_AUTHORING_CONTEXT.json → section_data in registry entry.
    Never silently returns empty list if claims are present.
    """
    work_dir = Path(section_meta.get("work_dir", ""))
    ctx_path = work_dir / "SECTION_AUTHORING_CONTEXT.json"
    if ctx_path.exists():
        try:
            ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
            # Context stores claims under section_data.claims
            claims = (ctx.get("section_data") or {}).get("claims") or ctx.get("claims", [])
            ids = [c["claim_id"] for c in claims if c.get("load_bearing") and c.get("claim_id")]
            if ids:
                return ids
        except Exception:
            pass
    # Fallback: section_data injected in registry entry (may be None)
    sd = section_meta.get("section_data") or {}
    return [c["claim_id"] for c in sd.get("claims", [])
            if c.get("load_bearing") and c.get("claim_id")]


# Typed callables — explicit injection avoids dynamic attribute lookup on config
SectionRerunCallable = Callable[[str, Dict[str, Any]], bool]


@dataclass
class ReauditResult:
    """Result of a post-revision citation re-audit for one section."""
    passed: bool
    reason: str = ""


# Callable signature for section citation re-audit injection
SectionReauditCallable = Callable[[str], ReauditResult]


@dataclass
class RevisionTransactionRecord:
    """Immutable record of one revision attempt — written to REVISION_HISTORY.json."""

    revision_id: str
    affected_sections: List[str]
    before_hash: str
    after_hash: str
    changed_word_count: int
    claims_before: Dict[str, List[str]]
    claims_after: Dict[str, List[str]]
    citations_before: Dict[str, List[str]]
    citations_after: Dict[str, List[str]]
    validation_status: str  # "passed" | "failed"
    citation_reaudit_status: str  # "passed" | "failed" | "skipped"
    claim_coverage_before: Dict[str, List[str]]  # sid -> [claim_id, ...]
    claim_coverage_after: Dict[str, List[str]]
    restored_artifacts: List[str]  # artifact names restored on rollback
    side_effects: List[str]
    committed: bool
    rollback_reason: Optional[str]
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RevisionResult:
    """Result of applying one revision plan."""

    round_num: int
    flags_before: int
    flags_after: int
    applied_revisions: List[str]
    skipped_revisions: List[str]
    sections_rerun: List[str]
    side_effect_warnings: List[str]

    @property
    def improvement_ratio(self) -> float:
        if self.flags_before == 0:
            return 0.0
        return (self.flags_before - self.flags_after) / self.flags_before


class TargetedRevisionWorker:
    """Applies revision plans. Inline patches are transactional; reruns use ResearchWorker."""

    def apply(
        self,
        revision_plan: Dict[str, Any],
        section_registry: Dict[str, Any],
        work_dir: Path,
        rerun_fn: Optional[SectionRerunCallable] = None,
        reaudit_fn: Optional[SectionReauditCallable] = None,
    ) -> RevisionResult:
        """Apply a revision plan and return the result."""
        round_num = revision_plan.get("round", 1)
        revisions = revision_plan.get("revisions", [])
        flags_before = len(revision_plan.get("auto_resolvable_flags", [])) + len(revision_plan.get("human_review_flags", []))

        applied = []
        skipped = []
        rerun_sections = []
        side_effect_warnings = []

        section_lookup = {s["section_id"]: s for s in section_registry.get("sections", [])}

        for revision in revisions:
            action = revision.get("action", "")
            flag_id = revision.get("flag_id", "")
            requires_rerun = revision.get("requires_rerun", False)
            target_sections = revision.get("target_sections", [])

            try:
                if requires_rerun and rerun_fn is not None:
                    for sid in target_sections[:1]:
                        section_meta = section_lookup.get(sid)
                        if section_meta:
                            success = rerun_fn(sid, revision)
                            if success:
                                applied.append(flag_id)
                                rerun_sections.append(sid)
                            else:
                                skipped.append(flag_id)
                else:
                    success = self._apply_inline_transactional(
                        action, target_sections, section_lookup, work_dir, revision,
                        reaudit_fn=reaudit_fn,
                    )
                    if success:
                        applied.append(flag_id)
                    else:
                        skipped.append(flag_id)

            except Exception as exc:
                logger.warning("revision %s failed: %s", flag_id, exc)
                skipped.append(flag_id)

        # Re-merge draft after revisions
        try:
            self._remerge_draft(section_registry, work_dir)
        except Exception as exc:
            logger.warning("remerge after revision failed: %s", exc)

        flags_after = flags_before - len(applied)

        return RevisionResult(
            round_num=round_num,
            flags_before=flags_before,
            flags_after=max(0, flags_after),
            applied_revisions=applied,
            skipped_revisions=skipped,
            sections_rerun=rerun_sections,
            side_effect_warnings=side_effect_warnings,
        )

    # ------------------------------------------------------------------
    # Transactional inline patch
    # ------------------------------------------------------------------

    def _apply_inline_transactional(
        self,
        action: str,
        target_sections: List[str],
        section_lookup: Dict[str, Any],
        work_dir: Path,
        revision: Dict[str, Any],
        reaudit_fn: Optional[SectionReauditCallable] = None,
    ) -> bool:
        """Wrap _apply_inline in a RevisionTransaction: snapshot → apply → validate → reaudit → commit/rollback.

        Snapshots 4 artifacts per section: draft, citation_map, authoring_audit, authoring_package.
        On reaudit failure all 4 are restored atomically.
        """
        if action == "remove_duplicate_paragraph":
            affected = [target_sections[-1]] if len(target_sections) >= 2 else []
        elif action in ("standardize_term_usage", "append_conceptual_figure_request"):
            affected = list(target_sections)
        else:
            affected = list(target_sections)

        if not affected:
            return self._apply_inline(action, target_sections, section_lookup, work_dir, revision)

        # --- snapshot 4 artifacts per section ---
        _SNAPSHOT_FILES = [
            "SECTION_DRAFT_EN.md",
            "SECTION_CITATION_MAP.json",
            "SECTION_AUTHORING_AUDIT.json",
            "SECTION_AUTHORING_PACKAGE.json",
        ]
        snapshots: Dict[str, Dict[str, Optional[str]]] = {}
        for sid in affected:
            meta = section_lookup.get(sid)
            if meta:
                swd = Path(meta["work_dir"])
                snapshots[sid] = {}
                for fname in _SNAPSHOT_FILES:
                    p = swd / fname
                    snapshots[sid][fname] = p.read_text(encoding="utf-8") if p.exists() else None

        draft_snapshots = {sid: (snapshots[sid].get("SECTION_DRAFT_EN.md") or "") for sid in affected}
        before_hash = _md5("|".join(draft_snapshots.values()))
        claim_coverage_before = {sid: _extract_load_bearing_claim_ids(section_lookup.get(sid, {}))
                                  for sid in affected}
        citations_before = {sid: _extract_ref_mentions(draft_snapshots.get(sid, ""))
                            for sid in affected}

        # --- apply ---
        try:
            success = self._apply_inline(action, target_sections, section_lookup, work_dir, revision)
        except Exception as exc:
            record = self._make_record(
                affected, draft_snapshots, draft_snapshots,
                claim_coverage_before, claim_coverage_before,
                citations_before, citations_before,
                validation_status="failed", citation_reaudit_status="skipped",
                committed=False, rollback_reason=str(exc),
            )
            self._append_history(work_dir, record)
            return False

        if not success:
            return False

        # --- read modified draft state ---
        modified_drafts: Dict[str, str] = {}
        for sid in affected:
            meta = section_lookup.get(sid)
            if meta:
                p = Path(meta["work_dir"]) / "SECTION_DRAFT_EN.md"
                modified_drafts[sid] = p.read_text(encoding="utf-8") if p.exists() else ""

        after_hash = _md5("|".join(modified_drafts.values()))
        citations_after = {sid: _extract_ref_mentions(modified_drafts.get(sid, "")) for sid in affected}
        claim_coverage_after = {sid: _extract_load_bearing_claim_ids(section_lookup.get(sid, {}))
                                 for sid in affected}

        # --- validate ---
        errors = self._validate_revision(affected, draft_snapshots, modified_drafts, section_lookup)
        changed_words = sum(
            abs(_word_count(modified_drafts.get(sid, "")) - _word_count(draft_snapshots.get(sid, "")))
            for sid in affected
        )

        def _restore_all(sids, reason=""):
            """Restore all 4 snapshot artifacts and remove any stale markers."""
            restored = []
            for sid in sids:
                meta = section_lookup.get(sid)
                if not meta:
                    continue
                swd = Path(meta["work_dir"])
                for fname in _SNAPSHOT_FILES:
                    p = swd / fname
                    snap = snapshots.get(sid, {}).get(fname)
                    if snap is not None:
                        p.write_text(snap, encoding="utf-8")
                        restored.append(f"{sid}/{fname}")
                    elif p.exists():
                        p.unlink()  # file didn't exist before — remove it
                stale = swd / ".citation_audit_stale"
                if stale.exists():
                    stale.unlink()
            return restored

        if errors:
            restored = _restore_all(affected)
            record = self._make_record(
                affected, draft_snapshots, draft_snapshots,
                claim_coverage_before, claim_coverage_before,
                citations_before, citations_before,
                validation_status="failed", citation_reaudit_status="skipped",
                changed_word_count=0, committed=False,
                rollback_reason="; ".join(errors), restored_artifacts=restored,
            )
            self._append_history(work_dir, record)
            logger.warning("revision rolled back for sections %s: %s", affected, "; ".join(errors))
            return False

        # --- write stale marker then call reaudit ---
        for sid in affected:
            meta = section_lookup.get(sid)
            if meta and modified_drafts.get(sid, "") != draft_snapshots.get(sid, ""):
                (Path(meta["work_dir"]) / ".citation_audit_stale").write_text("stale", encoding="utf-8")

        reaudit_status = "skipped"
        if reaudit_fn is not None:
            for sid in affected:
                meta = section_lookup.get(sid)
                if not meta or modified_drafts.get(sid) == draft_snapshots.get(sid):
                    continue
                try:
                    result = reaudit_fn(sid)
                    if result.passed:
                        reaudit_status = "passed"
                    else:
                        reaudit_status = "failed"
                        restored = _restore_all(affected)
                        record = self._make_record(
                            affected, draft_snapshots, draft_snapshots,
                            claim_coverage_before, claim_coverage_before,
                            citations_before, citations_before,
                            validation_status="passed", citation_reaudit_status="failed",
                            changed_word_count=0, committed=False,
                            rollback_reason=f"citation_reaudit failed for {sid}: {result.reason}",
                            restored_artifacts=restored,
                        )
                        self._append_history(work_dir, record)
                        logger.warning("reaudit failed for %s — transaction rolled back: %s", sid, result.reason)
                        return False
                except Exception as exc:
                    reaudit_status = "failed"
                    restored = _restore_all(affected)
                    record = self._make_record(
                        affected, draft_snapshots, draft_snapshots,
                        claim_coverage_before, claim_coverage_before,
                        citations_before, citations_before,
                        validation_status="passed", citation_reaudit_status="failed",
                        changed_word_count=0, committed=False,
                        rollback_reason=f"reaudit exception for {sid}: {exc}",
                        restored_artifacts=restored,
                    )
                    self._append_history(work_dir, record)
                    return False

        # --- committed ---
        record = self._make_record(
            affected, draft_snapshots, modified_drafts,
            claim_coverage_before, claim_coverage_after,
            citations_before, citations_after,
            validation_status="passed", citation_reaudit_status=reaudit_status,
            changed_word_count=changed_words, committed=True, rollback_reason=None,
        )
        self._append_history(work_dir, record)
        return True


    def _validate_revision(
        self,
        affected: List[str],
        before: Dict[str, str],
        after: Dict[str, str],
        section_lookup: Dict[str, Any],
    ) -> List[str]:
        """Return list of error strings; empty = safe to commit."""
        errors: List[str] = []
        for sid in affected:
            before_text = before.get(sid, "")
            after_text = after.get(sid, "")
            before_words = _word_count(before_text)
            after_words = _word_count(after_text)

            # Guard 1: section must not be empty
            if not after_text.strip():
                errors.append(f"{sid}: revision would empty the section")
                continue

            # Guard 2: minimum absolute word count
            if after_words < _MIN_SECTION_WORDS:
                errors.append(
                    f"{sid}: post-revision word count {after_words} < minimum {_MIN_SECTION_WORDS}"
                )

            # Guard 3: retention ratio (≥60% of original)
            if before_words > 0:
                ratio = after_words / before_words
                if ratio < _MIN_RETENTION_RATIO:
                    errors.append(
                        f"{sid}: post-revision retains only {ratio:.0%} of original "
                        f"({after_words}/{before_words} words); minimum is {_MIN_RETENTION_RATIO:.0%}"
                    )

            # Guard 4: load-bearing claim IDs must still be covered (section must not be emptied)
            meta = section_lookup.get(sid, {})
            claim_ids = _extract_load_bearing_claim_ids(meta)
            if claim_ids and not after_text.strip():
                errors.append(f"{sid}: revision removed content needed to cover load-bearing claims {claim_ids}")

            # Guard 5: no [REF:*] citations may disappear entirely (if there were any before)
            refs_before = set(_extract_ref_mentions(before_text))
            refs_after = set(_extract_ref_mentions(after_text))
            lost_refs = refs_before - refs_after
            if lost_refs:
                errors.append(
                    f"{sid}: revision removed citation(s): {', '.join(sorted(lost_refs))}"
                )

        return errors

    def _make_record(
        self,
        affected: List[str],
        before: Dict[str, str],
        after: Dict[str, str],
        claims_before: Optional[Dict[str, List[str]]] = None,
        claims_after: Optional[Dict[str, List[str]]] = None,
        citations_before: Optional[Dict[str, List[str]]] = None,
        citations_after: Optional[Dict[str, List[str]]] = None,
        validation_status: str = "failed",
        citation_reaudit_status: str = "skipped",
        changed_word_count: int = 0,
        committed: bool = False,
        rollback_reason: Optional[str] = None,
        restored_artifacts: Optional[List[str]] = None,
    ) -> RevisionTransactionRecord:
        return RevisionTransactionRecord(
            revision_id=uuid.uuid4().hex[:12],
            affected_sections=list(affected),
            before_hash=_md5("|".join(before.get(s, "") for s in affected)),
            after_hash=_md5("|".join(after.get(s, "") for s in affected)),
            changed_word_count=changed_word_count,
            claims_before=claims_before or {},
            claims_after=claims_after or {},
            citations_before=citations_before or {},
            citations_after=citations_after or {},
            validation_status=validation_status,
            citation_reaudit_status=citation_reaudit_status,
            claim_coverage_before=claims_before or {},
            claim_coverage_after=claims_after or {},
            restored_artifacts=restored_artifacts or [],
            side_effects=[],
            committed=committed,
            rollback_reason=rollback_reason,
            created_at=_now(),
        )

    def _append_history(self, work_dir: Path, record: RevisionTransactionRecord) -> None:
        """Append transaction record to REVISION_HISTORY.json."""
        history_path = work_dir / "REVISION_HISTORY.json"
        try:
            if history_path.exists():
                history = json.loads(history_path.read_text(encoding="utf-8"))
            else:
                history = {"schema_version": "phase4.revision_history.v1", "records": []}
            history["records"].append(record.to_dict())
            history_path.write_text(json.dumps(history, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("could not write REVISION_HISTORY.json: %s", exc)

    # ------------------------------------------------------------------
    # Inline patch implementations
    # ------------------------------------------------------------------

    def _apply_inline(
        self,
        action: str,
        target_sections: List[str],
        section_lookup: Dict[str, Any],
        work_dir: Path,
        revision: Dict[str, Any],
    ) -> bool:
        if action == "remove_duplicate_paragraph":
            return self._remove_duplicate_content(target_sections, section_lookup)
        elif action == "standardize_term_usage":
            canonical = revision.get("canonical_term", "")
            variant = revision.get("variant_found", "")
            return self._standardize_term(target_sections, section_lookup, canonical, variant)
        elif action == "append_conceptual_figure_request":
            return self._append_figure_request(target_sections, section_lookup, work_dir)
        return False

    def _remove_duplicate_content(
        self, target_sections: List[str], section_lookup: Dict[str, Any]
    ) -> bool:
        """Remove only exact duplicate sentences from the lower-priority section.

        Safety: never removes a sentence that is the only citation or claim reference.
        Never removes more than individual sentences (not whole paragraphs).
        Whole-paragraph removal falls back to section_rewrite (human_review) instead.
        """
        if len(target_sections) < 2:
            return False

        target_id = target_sections[-1]
        source_id = target_sections[0]

        target_meta = section_lookup.get(target_id)
        source_meta = section_lookup.get(source_id)
        if not target_meta or not source_meta:
            return False

        target_draft = Path(target_meta["work_dir"]) / "SECTION_DRAFT_EN.md"
        source_draft = Path(source_meta["work_dir"]) / "SECTION_DRAFT_EN.md"
        if not target_draft.exists() or not source_draft.exists():
            return False

        source_text = source_draft.read_text(encoding="utf-8")
        target_text = target_draft.read_text(encoding="utf-8")

        # Split into individual sentences while retaining every original
        # whitespace separator. Revision must never flatten Markdown
        # paragraphs into a single wall of text.
        source_sentences = {
            sentence.strip()
            for sentence in re.split(r'(?<=[.!?])\s+', source_text)
            if len(sentence.strip()) > 30
        }
        source_clean = {
            re.sub(r'\[REF:[^\]]+\]', '', sentence).strip()
            for sentence in source_sentences
        }

        # Remove only exact matches — sentence must appear verbatim in source
        target_parts = re.split(r'((?<=[.!?])\s+)', target_text)
        kept: List[str] = []
        removed_any = False
        for index in range(0, len(target_parts), 2):
            sent = target_parts[index]
            separator = target_parts[index + 1] if index + 1 < len(target_parts) else ""
            stripped = sent.strip()
            # Remove [REF:*] for comparison purposes only
            sent_clean = re.sub(r'\[REF:[^\]]+\]', '', stripped).strip()

            if stripped and sent_clean in source_clean and len(sent_clean) > 30:
                removed_any = True
                if "\n" in separator:
                    kept.append(separator)
            else:
                kept.extend([sent, separator])

        if not removed_any:
            return False

        new_text = "".join(kept).strip()
        # Pre-validate: don't write if result would be empty or too short
        if not new_text.strip() or _word_count(new_text) < _MIN_SECTION_WORDS:
            return False

        target_draft.write_text(new_text, encoding="utf-8")
        return True

    def _standardize_term(
        self,
        target_sections: List[str],
        section_lookup: Dict[str, Any],
        canonical: str,
        variant: str,
    ) -> bool:
        if not canonical or not variant:
            return False

        changed_any = False
        for section_id in target_sections:
            section_meta = section_lookup.get(section_id)
            if not section_meta:
                continue
            draft_path = Path(section_meta["work_dir"]) / "SECTION_DRAFT_EN.md"
            if not draft_path.exists():
                continue
            text = draft_path.read_text(encoding="utf-8")
            new_text = re.sub(re.escape(variant), canonical, text, flags=re.IGNORECASE)
            if new_text != text:
                draft_path.write_text(new_text, encoding="utf-8")
                changed_any = True

        return changed_any

    def _append_figure_request(
        self,
        target_sections: List[str],
        section_lookup: Dict[str, Any],
        work_dir: Path,
    ) -> bool:
        requests_path = work_dir / "CONCEPTUAL_FIGURE_REQUESTS.json"
        try:
            if requests_path.exists():
                existing = json.loads(requests_path.read_text(encoding="utf-8"))
            else:
                existing = {"schema_version": "phase4.figure_requests.v1", "requests": []}

            already_requested = {
                str(item.get("section_id"))
                for item in existing.get("requests", [])
                if isinstance(item, dict) and item.get("section_id")
            }
            for section_id in target_sections:
                if section_id in already_requested:
                    continue
                existing["requests"].append({
                    "section_id": section_id,
                    "reason": "visual_gap detected by GlobalReviewAuditor",
                    "requested_at": _now(),
                })
                already_requested.add(section_id)

            requests_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
            return True
        except Exception:
            return False

    def _remerge_draft(self, section_registry: Dict[str, Any], work_dir: Path) -> None:
        """Re-merge section drafts into FULL_REVIEW_DRAFT_EN.md after inline patches."""
        sections = section_registry.get("sections", [])
        parts = []
        for section in sections:
            section_id = section["section_id"]
            section_work_dir = Path(section.get("work_dir", ""))
            draft_path = section_work_dir / "SECTION_DRAFT_EN.md"
            if draft_path.exists():
                title = section.get("title", section_id)
                text = _strip_repeated_leading_title(
                    draft_path.read_text(encoding="utf-8"),
                    title,
                )
                text = _demote_embedded_section_headings(text)
                parts.append(f"## {title}\n\n{text}")

        merged = '\n\n---\n\n'.join(parts)
        merged_path = work_dir / "FULL_REVIEW_DRAFT_EN.md"
        merged_path.write_text(merged, encoding="utf-8")
