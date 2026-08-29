---
name: manuscript-integration
description: Command knowledge for deterministic manuscript assembly from frozen section blocks, including content hashing, block IDs, structured patches, authorization, fail-closed application, and rollback.
---

# Manuscript Integration - Command Knowledge

STATUS: This manual is dense command knowledge for the manuscript assembler role. It is not scientific evidence and contains no citations, measurements, or pre-loaded answers. Assembly is mechanical and deterministic; all scientific facts remain owned by the evidence pipeline.

## 1. Inputs

Assemble only from frozen, registered blocks:
- the article blueprint and chapter roster (ownership, order, transitions);
- each section's SECTION_DRAFT_EN.md, input_packet.json, writing_result.json, acceptance_summary.json, SECTION_HANDOFF_CARD.json, SECTION_CITATION_AUDIT.json, SECTION_VISUAL_PLACEMENT.json;
- the global audit report and its structured work orders;
- the completed introduction, abstract, outlook, and conclusion components when they exist.

Never assemble from unregistered Markdown files or from prose reconstructed by a model.

## 2. Freeze, hash, and block IDs

1. A block is the smallest unit of assembly: one section draft plus its structured ledger, identified by a stable block ID (section_id plus version).
2. Compute a content hash over the block's canonical serialization (draft text plus ledger references) at freeze time; the hash is the block's identity evidence.
3. Register every block in a frozen manifest before assembly. Unregistered text, unknown block IDs, and hash mismatches fail closed.
4. Any content change produces a new block version with a new hash and requires claim/citation re-review before it may be registered. The previous version remains for rollback.

## 3. Deterministic assembly

1. Assemble in the blueprint's chapter order using the registered components: title, abstract, introduction, body blocks, outlook, conclusion.
2. Composition is mechanical: concatenate frozen blocks with their canonical headers; strip a leading duplicate title only when the component is known to carry one.
3. Do not ask a model to rewrite, merge, or polish during assembly. Polish is a separate, authorized pass over blocks; it never changes claims, numbers, citations, or evidence scope.
4. Emit a machine-readable manifest recording component order, every block ID, the visual placement count, the word count, and the source artifact paths.
5. Missing or invalid blocks stop assembly at the first defect; never assemble a partial article silently.

## 4. Overlap resolution

1. Consume the global audit's overlap flags before assembly or re-assembly.
2. Resolve overlap only through structured work orders: keep, move, merge, delete, or rewrite_request, each with block IDs and affected claim IDs.
3. Same-paper reuse across blocks is allowed only when the paper performs a different scientific role; record the role in the work order.
4. A merge moves content under the owning block; it does not delete claims unless the work order says so.

## 5. Structured patches

A patch is data, never prose:
```json
{
  "patch_id": "<local id>",
  "action": "keep|move|merge|delete|rewrite_request",
  "block_id": "<section_id>.<version>",
  "target_block_id": "<section_id>.<version>",
  "affected_claim_ids": ["<claim_id>"],
  "rationale": "<why>",
  "requires_evidence_review": true,
  "authorization": "commander|section_author|polisher"
}
```
Every claim-altering patch lists its affected claim IDs and sets requires_evidence_review. Missing fields are a validation error.

## 6. Authorization

1. The global commander may issue structural work orders (move, merge, delete, order changes).
2. Section authors may revise only their own sections using their evidence packets; a rewrite_request returns to the owning section, it is not performed by the assembler.
3. The polisher may remove repetition, smooth transitions, and fix terminology only. It may never change numbers, claims, citations, or evidence scope.
4. A patch without recorded authorization is rejected.

## 7. Fail-closed application

1. Before applying any patch, validate: block ID exists in the frozen manifest; the content hash matches; every affected claim ID exists; requires_evidence_review is true when claims or evidence change; the authorization is recorded; the action is allowed for that actor.
2. On the first validation failure, stop the round, record the rejection in the audit trail, and apply nothing further.
3. Never apply a fallback that silently ignores a failed patch; a failed application is a failed round.

## 8. Claim/evidence re-review

1. Before a claim-altering patch is applied, every affected claim re-enters evidence re-review: unsupported, partially_supported, contested, open_question, or ready_for_write.
2. Explicitly deleted claims take priority over improved_stop or optimistic states; they must not leak back into prose.
3. After application, the affected sections' citation audits must be re-run; a stale citation audit is a blocking defect.

## 9. Rollback

1. Keep every prior frozen manifest and the registered blocks they reference.
2. If a patch round fails validation or its post-application audit fails, restore the last hash-verified manifest and record the rollback.
3. Rollback restores blocks; authorized deletions that were recorded are not resurrected by rollback.

## 10. Validation and stop conditions

The article is assembled only when:
- every block is frozen, hashed, and registered;
- the manifest lists all component order and block IDs;
- no overlap work order is unresolved without a recorded acceptance;
- no patch is pending, unauthorized, or unvalidated;
- the deterministic assembly validator reports VALIDATION_PASSED.

Stop when any one of the following is true:
- VALIDATION_PASSED with a complete manifest;
- a blocking defect stops the round and the failure is recorded with the offending block IDs;
- budget is reached and the partial manifest honestly records every missing block.

## Provenance

This skill is versioned command knowledge. Machine-readable provenance, including source project, commit, attribution metadata, and adoption modes, is in `provenance.json` in this directory. Downstream code must load it through the skill-guidance contract so it is never mistaken for scientific evidence.
