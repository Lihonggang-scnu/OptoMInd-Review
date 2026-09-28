---
name: global-review-audit
description: Command knowledge for auditing a multi-section scientific review as one article, covering frozen blocks, overlap detection, claim/evidence re-review, structured work orders, authorization, and fail-closed validation.
---

# Global Review Audit — Command Knowledge

STATUS: This manual is dense command knowledge for the article-level auditor (managing editor). It is not scientific evidence and contains no citations, measurements, or pre-loaded answers. Article-level judgments are structural and editorial; every scientific fact remains owned by the evidence pipeline.

## 1. Load the article map

1. Call `load_global_review_context` first. Treat the returned paragraph map, citation-concentration statistics, section statuses, and visual placement counts as the primary audit evidence.
2. Read only the one or two section windows needed to verify a material article-level problem, using `read_section_text` within its bounded budget. Do not read the article section by section.
3. If an editorial pattern would benefit from a transferable move, call `consult_review_mentor_for_audit`. Mentor records are abstract editorial heuristics only; they are never scientific evidence and must never be cited.

## 2. Audit dimensions

Check, in this order of importance:
1. Missing main line: does the article as a whole advance the fixed thesis, or do sections read as independent mini-reviews?
2. Chapter ordering and progression: does each chapter hand off to the next, or do handoffs break?
3. Overlap: near-verbatim reuse across sections, and duplicated argument roles with high chunk overlap.
4. Concept drift and classification confusion: does each section keep its taxonomy role, or do sections reclassify the same concept differently?
5. Generic synthesis over-concentration: is the same generic overview cited across many chapters without performing distinct roles?
6. Terminology ledger violations: does each section use the canonical term?
7. Citation validity: unknown references, orphaned chunk IDs, and stale citation audits.
8. Visual gaps: planned visual_argument_slots with no placements, and unanswered conceptual requests.
9. Artifact completeness and language integrity: missing authoring artifacts, empty or too-short drafts, stale authoring packages, CJK leakage in English drafts.

## 3. Root-cause flags over symptoms

1. Prefer a small set of root-cause flags over many sentence-level symptoms.
2. Each flag carries type, severity, section_ids, description, blocking, root_cause, and recommended_action.
3. Only flag what the compact map or a bounded section read supports; do not invent problems the evidence does not show.
4. Submit flags through `submit_audit_flags` and let the deterministic validator check structure, section IDs, severity, and language.

## 4. Evidence discipline

1. Treat synthesis as legitimate author reasoning. Reserve strict evidence objections for measurements, explicit comparisons, strong causality, and source-specific facts.
2. Do not flag reasonable cross-section terminology reuse as duplication; near-verbatim reuse and identical argument roles with high chunk overlap are the material defects.
3. A limitation alone is not controversy; conflicting evidence or disputed definitions is.
4. M1 mentor moves and this manual are command knowledge; neither is admissible as evidence for or against a scientific claim.

## 5. Freeze, hash, and block IDs

1. Every accepted section is a frozen block identified by a stable block ID (section_id plus version) and a content hash.
2. The audit operates on frozen blocks only. Unregistered text, changed hashes, and unknown block IDs are audit defects and fail closed.
3. Any content change creates a new block version and requires re-audit of that block's claims and citations; the old block remains for rollback.
4. Assembly mechanics are owned by the manuscript-integration skill; this skill owns the audit side of those mechanics.

## 6. Overlap detection and structured work orders

1. Detect overlap from the block registry: duplicated chunks, same argument role with high Jaccard overlap, and near-verbatim sentence reuse.
2. Same-paper reuse across chapters is legitimate only when the paper performs a different scientific role; record the role each occurrence performs.
3. Express every proposed fix as a structured work order: action (keep, move, merge, delete, rewrite_request), block IDs, affected claim IDs, rationale, and required evidence re-review. Work orders are data, not prose edits.
4. Never propose deleting content to inflate diversity; deletion is justified only by genuine duplication, unsupported claims, or ownership violation.

## 7. Claim/evidence re-review

1. A work order that changes claims, removes evidence, or rewrites a section must list every affected claim ID.
2. Affected claims re-enter the evidence status chain (unsupported, partially_supported, contested, open_question, ready_for_write) before the patch may apply.
3. Explicitly deleted claims take priority over outer improved_stop states: a deleted claim must not leak back into prose.
4. After application, re-run the citation audit for every affected section; a stale audit is a blocking defect.

## 8. Authorization and fail-closed application

1. The global commander may issue structural work orders (move, merge, delete, order changes). Section authors may revise only their own sections with their evidence packets. The polisher may only remove repetition, smooth transitions, and fix terminology; it must never change numbers, claims, citations, or evidence scope.
2. No unauthenticated or undocumented patch may be applied. Unknown block IDs, hash mismatches, missing claim IDs, and unauthorized actions reject the patch and stop the round.
3. A validator failure is a hard stop, not a reason to apply a partial or silent fallback.

## 9. Rollback

1. Keep every prior frozen manifest. On a failed or rejected application, restore the last hash-verified manifest.
2. Record all applications, rejections, and rollbacks in an append-only audit trail; never silently overwrite a frozen block.
3. Rollback restores blocks; it does not restore claims that were deleted with authorization and recorded.

## 10. Validation and stop conditions

Call `validate_global_audit_package` after submitting flags. The audit is complete when:
- the compact map was loaded and at most the bounded section reads were used;
- the smallest sufficient root-cause flag set was submitted;
- every blocking flag is either resolved, converted into a structured work order, or explicitly accepted for a later round;
- no unknown block IDs, changed hashes, or stale audits remain;
- the deterministic validator returned VALIDATION_PASSED.

Stop when any one of the following is true:
- VALIDATION_PASSED with no unresolved blocking flags;
- all blocking defects are recorded as structured work orders and the validator accepts the package;
- the inspection budget is exhausted and the supported flags are recorded honestly.

## Provenance

This skill is versioned command knowledge. Machine-readable provenance, including source project, commit, attribution metadata, and adoption modes, is in `provenance.json` in this directory. Downstream code must load it through the skill-guidance contract so it is never mistaken for scientific evidence.
