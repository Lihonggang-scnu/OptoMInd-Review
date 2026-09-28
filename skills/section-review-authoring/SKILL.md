---
name: section-review-authoring
description: Command knowledge for authoring one section of a scientific review from approved claims and evidence, consuming all sibling outlines, and enforcing claim/evidence and unsupported-claim rules.
---

# Section Review Authoring — Command Knowledge

STATUS: This manual is dense command knowledge for the section author role. It is not scientific evidence and contains no citations, measurements, or pre-loaded answers. The only admissible scientific facts are the retrieved chunks, papers, and verified materials provided by the coverage and evidence pipeline.

## Step 0 — Consume the full architecture and every sibling outline

1. Read the complete review architecture before writing: the review-wide thesis, taxonomy principle, chapter roster, and this chapter's contract.
2. Read every sibling chapter's outline: responsibility, questions, must_not_cover, transition contracts, and handoffs. Consuming all sibling outlines is mandatory, not optional.
3. Build this chapter's must_not_cover list from the architecture. A topic owned by a sibling chapter is forbidden here; do not duplicate it, do not re-introduce its background, and do not steal its claims.
4. Honor handoffs: established takeaways from the previous chapter are not re-established; the forward question handed to the next chapter is not answered here.
5. If the material forces you to touch a sibling-owned topic, stop that paragraph and reference the owning chapter instead of covering it.

## Step 1 — Load and understand the authoring context

Call `load_authoring_context` first. Read:
- the chapter argument (what claim this section must support);
- the coverage_status and blocking_gaps_remain flag;
- available sources per coverage role and their chunk_ids;
- visual chunk IDs available for this section;
- full-review argument, section role, neighboring-section responsibilities, transition contract, terminology ledger, mentor advice, and the complete Phase-2 gap report.

Mentor advice and this skill are command knowledge. They are never scientific evidence.

Do not write until you understand both the section's argumentative job and the range of evidence available. Evidence is material for reasoning, not a set of sentences to concatenate.

## Step 2 — Inspect materials

Call `inspect_material_package` to get a role-by-role breakdown of chunks and papers. Read and obey the returned section contract, claims, and scope guardrails.

For pivotal chunks used for measurements, strong causal claims, explicit comparisons, or source-specific facts, call `retrieve_chunk_text` with the relevant chunk_ids. Read the actual text before making those claims. Batch selected IDs into one call and do not request successfully returned IDs again.

Call `inspect_visual_assets` once to list the top available figures and their argument types.

Enforce source diversity: meet the minimum unique sources and minimum direct sources returned by the context. A one-paper section is a coverage gap, not a stylistic choice. When the targets cannot be met, record the coverage_breadth gap rather than presenting a single source as comprehensive.

## Step 3 — Plan the argument

Call `submit_argument_plan` with a paragraph-level outline:
- one entry per paragraph;
- each paragraph has: function, topic_sentence, evidence_chunk_ids, writing_permission.

`writing_permission` must honestly reflect what the evidence can support:
- `factual_assertion`: chunk directly entails the claim;
- `hedged_factual_assertion`: chunk partially supports; must hedge language;
- `interpretive_synthesis`: a cross-source conclusion; attach the synthesized chunks;
- `common_background`: non-numeric, non-causal established context;
- `structural_transition`: navigation between arguments, not a scientific claim;
- `evidence_gap_only`: no chunk; note the gap, do not assert.

Interpretive synthesis may be built from the overall literature pattern without a word-for-word source, provided it introduces no invented result, number, or experiment and its uncertainty is expressed honestly. Keep the sum of paragraph word targets within the supplied section word budget.

## Step 4 — Build the evidence packet

Call `build_evidence_packet` with every chunk you will cite:
- include `claim_ids`, `writing_permission`, and a concise `support_hint`;
- omit `exact_spans` in the normal workflow; the deterministic resolver selects verbatim passages from canonical chunk text and rejects unrelated chunks;
- use only paper/chunk pairs returned by the canonical context; the chunk must belong to the paper;
- mark `not_usable_for` for chunks that cannot support exact measurements or causation;
- list `claim_ids` from the blueprint claims this chunk addresses.

## Step 5 — Write the draft

Call `submit_section_draft` with the full English prose.

Writing rules:
- Use `[REF:paper_id]` for sentences that assert a specific fact, measurement, or experimental result traceable to a single source. One marker per source per sentence.
- Do NOT add `[REF:paper_id]` to every sentence. Background statements, logical inferences spanning multiple sources, and synthesis across the argument do not require per-sentence markers.
- Advance the assigned chapter argument rather than reintroducing the review topic in every section. Avoid formulaic "not X, but Y" contrasts unless X is an actual position documented in the literature.
- Never use `[REF:UNKNOWN]` as a placeholder — leave the claim out or hedge it.
- Never write a number, percentage, or measurement not present in a retrieved chunk.
- Do not reproduce more than 30 consecutive words verbatim from any chunk.
- Do not import topic facts from M1 mentor moves, from this manual, or from any command knowledge; command knowledge is never evidence.
- If the runtime uses short evidence handles, use only handles returned by the local registry; never invent or guess handle IDs.
- Academic register: concrete, precise, no filler hedges.

Unsupported-claim rules:
- A claim with no chunk is `evidence_gap_only`, never a `factual_assertion`.
- A partially supported claim must carry the strongest hedge the evidence allows.
- Deleted or unsupported claims must not leak back into the draft.
- Unknown references, invented measurements, and fabricated experiments are blocking defects.

## Step 6 — Audit citations and claims

Call `run_citation_audit` with the citation map:
- prefer passing an empty map; the deterministic tool reconstructs bindings from draft markers and canonical assets;
- map every sentence containing a marker to its chunk_ids and paper_ids;
- use a distinctive `sentence_snippet` as the stable locator; do not manually guess sentence numbers;
- do not omit marker-bearing sentences or pair one allowed paper with another paper's chunk;
- the tool independently recomputes mapping, provenance, scope, and support; author-supplied entailment labels are not authoritative;
- flag overclaims (numerical claim not entailed by chunk) as blocking;
- flag unknown references as blocking;
- flag scope violations as important.

## Step 7 — Resolve blocking flags

If `audit_passed` is false, call `submit_revision` with a corrected draft. Explain what was changed. Do not validate while blocking flags remain.

## Step 8 — Place visuals (if available)

Call `submit_visual_placement` only for candidates returned as placement-eligible. Use the canonical visual ID, paper ID, and image path. Parent figures, unreviewed kinds, non-decodable images, and non-relevant/non-reranked candidates cannot be placed. AI conceptual art remains pending until it exists as an independently reviewed canonical artifact. Omit `asset_status`; the deterministic tool computes `verified_local`.

## Step 9 — Submit the handoff card

After the draft is accepted, call `submit_section_handoff_card` with compact English takeaways for the next section: established takeaways, conditional judgments, unresolved tensions, terms defined, avoid_repeating, the forward question, and why the next section is needed. The tool recomputes used paper/chunk/visual IDs from the validated artifacts; do not invent IDs. The handoff card is how the architecture's sibling contracts are honored across sections.

## Step 10 — Request more literature if needed

If a gap prevents a pivotal numerical, causal, or comparative claim, call `request_more_literature`. A missing verbatim source for ordinary review-level synthesis is not by itself a blocking gap; use bounded language and state the remaining uncertainty.

## Step 11 — Validate

Call `validate_authoring_package` when the draft is complete and all blocking flags are resolved. Only declare the task complete after receiving `VALIDATION_PASSED`.

## Stop conditions (any one is sufficient)

- `VALIDATION_PASSED` received from `validate_authoring_package`.
- All required authoring artifacts exist and no blocking flags remain.
- `request_more_literature` was called for all blocking gaps and the insufficient-material authoring package validated, with or without a partial draft as the evidence permits.
- Budget reached (iterations, tokens, or wall time).

## Provenance

This skill is versioned command knowledge. Machine-readable provenance, including source project, commit, attribution metadata, and adoption modes, is in `provenance.json` in this directory. Downstream code must load it through the skill-guidance contract so it is never mistaken for scientific evidence.
