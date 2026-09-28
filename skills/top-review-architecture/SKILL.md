---
name: top-review-architecture
description: Command knowledge for designing a publication-oriented scientific review architecture from a fixed research brief, including candidate architectures, 8-10 chapter responsibilities, ownership boundaries, claim/evidence requirements, conflict/gap handling, validation, and stop conditions.
---

# Top Review Architecture — Command Knowledge

STATUS: This manual is dense command knowledge for the review architect role. It is not scientific evidence. It contains no citations, measurements, paper facts, or pre-loaded answers. Topic evidence is loaded separately by the retrieval and coverage pipeline and is the only admissible source of scientific facts.

## 0. Operating boundary

- Qwen owns the intellectual structure: review-wide thesis, taxonomy principle, chapter responsibilities, claim content, transitions, and structural judgments.
- Local deterministic code owns identity, format, budgets, routes, and validation. It never generates scientific structure.
- Preserve every established upstream contract: the research brief, query plan, topic identity, and coverage portfolio are inputs; they are not rewritten by this skill.

## 1. Research brief

1. Load the fixed user question, problem understanding, and scope definition first (`load_review_brief`).
2. Preserve the interpreted question and scope verbatim in the plan. Do not silently narrow, widen, or rephrase the question.
3. Record the methodology identity and visual policy from the brief; do not claim a systematic review unless a complete search, screening, extraction, and quality protocol exists.
4. Read the corpus overview (`inspect_review_knowledge_base`) to learn what material exists. The overview gives counts and themes, never citation evidence.
5. Consult the M1 mentor for writing moves only (`consult_review_mentor`): abstract the organizational move; never copy topic facts, citations, or conclusions from the mentor library. M1 and this skill are command knowledge; both are forbidden as scientific evidence.

## 2. Multiple candidate architectures

1. Generate at least three distinct candidate architectures before fixing one. Candidate architectures differ in thesis emphasis, taxonomy axis, chapter roster, and progression logic, not merely in chapter titles.
2. Each candidate must state:
   - the review-wide thesis it supports;
   - the taxonomy principle (the stable axis on which chapters divide the topic);
   - the chapter roster and the argument role of each chapter;
   - how the candidate maps to the available material (roles covered, expected coverage burden);
   - the progression logic from opening to conclusion;
   - its main risk (for example overlap, missing main line, or material shortfall).
3. Evaluate candidates against: scope coverage, material fit, non-overlap, reader progression, publishability, and recoverability (can each chapter be written and audited independently).
4. Select one candidate or a documented hybrid. Record the rejected alternatives and the reasons; do not silently discard a rejected architecture.
5. The local program validates structure and boundaries only. The choice of architecture is Qwen's intellectual judgment.

## 3. Chapter roster (8–10 chapters)

Produce a roster of 8–10 chapters unless the question and material justify a defensible different count; local rules never hard-cut the count. For every chapter assign all of the following:

- chapter_id (stable, locally generated or model-suggested and normalized by the program);
- working title;
- one-sentence responsibility;
- questions the chapter must answer;
- argument role in the article-wide thesis;
- synthesis task (what judgment the chapter must reach);
- literature coverage roles (foundation, mechanism, method, frontier, controversy, application) with required versus optional;
- evidence requirement tier (what kind and strength of evidence the chapter's claims need);
- transition contract from the previous chapter and to the next chapter;
- sibling workplan: which adjacent or overlapping topics belong to which other chapter;
- must_not_cover: explicit topics owned by sibling chapters that this chapter must not duplicate;
- handoffs: what this chapter establishes, what it deliberately leaves unresolved, and what the next chapter receives;
- realistic word range, treated as a soft target, never a padding requirement.

## 4. Sibling workplan, must_not_cover, and handoffs

1. Every scientific topic or sub-question is owned by exactly one chapter. Ownership is decided at architecture time and recorded in the chapter ledger.
2. For each chapter, write a must_not_cover list naming sibling-owned topics. A chapter that encounters a sibling-owned topic must not duplicate it; it references the handoff and moves on.
3. Write handoffs as explicit contracts: established takeaways, conditional judgments, unresolved tensions, terms defined, avoid-repeating list, and the forward question answered by the next chapter.
4. Cross-chapter reuse of the same paper is allowed only when the paper performs a different scientific role in each chapter. Cross-chapter reuse of the same claim is forbidden.
5. The architecture must expose the sibling outlines to the section authoring step so every section writer consumes all sibling outlines before writing.

## 5. Claim ownership

1. A claim is an atomic, one-owner unit: one claim, one chapter, one evidence obligation.
2. The local program creates claim IDs and status transitions; Qwen fills the high-information fields (claim text, role, evidence intent, limits).
3. Use the fixed status chain: planned, candidate_linked, quote_verified, permission_checked, ready_for_write. Failures enter unsupported, partially_supported, contested, or open_question; a claim that is explicitly deleted is never reinserted silently.
4. Every claim records the chapter that owns it, the paragraphs it may enter, and the evidence packet it consumes. A claim may not be written by a different chapter's author.
5. Background, counter-evidence, boundary, and visual material each have their own claim role; they cannot masquerade as positive support.

## 6. Evidence requirements and source diversity

1. Evidence tiers: verbatim full-text chunks and legal OA full text are strong evidence; verified abstract claims are limited background; metadata, recommendations, and TLDR are not evidence.
2. Set per-chapter evidence requirements before authoring: minimum unique sources, minimum direct sources, maximum single-source share, and the roles that require at least two independent sources.
3. Enforce source diversity at the architecture level: a chapter planned around one paper is a structural gap, not a writing problem. The single-source share is a planning constraint, not a post-hoc statistic.
4. Plan gaps explicitly: a chapter whose required roles cannot be filled from the coverage portfolio must be re-scoped, merged, or flagged for supplementary retrieval before authoring begins.
5. Command knowledge (this manual, M1 moves, methodology heuristics) is never admissible as scientific evidence.

## 7. Conflicts and gaps

1. Controversy means conflicting evidence, disputed definitions, or unresolved scientific disagreement; a limitation alone is not controversy.
2. Conflicts are presented with their boundaries: each position, its evidence strength, and what is genuinely unresolved. Never merge conflicting studies into a single unhedged conclusion.
3. Record gaps with the five-class task types used by the existing retrieval chain: claim_evidence_gap, section_argument_gap, review_structure_gap, whole_review_gap, visual_material_gap.
4. Gap tasks flow into the existing supplementary retrieval chain; they are never filled by inventing content, numbers, or citations.
5. A structural gap discovered at architecture time (missing chapter, duplicate role, unsupported backbone) is reported before authoring, not hidden until global audit.

## 8. Qwen intellectual ownership

1. Qwen owns the thesis, taxonomy, chapter responsibilities, transitions, claim content, synthesis judgments, and the choice among candidate architectures.
2. The local program owns IDs, schemas, budgets, routes, source permissions, and deterministic validation. It may correct field names and normalize formats, but it never generates or repairs scientific structure.
3. The architect must be able to explain why each chapter exists, why it is ordered as it is, and what judgment it must reach. These explanations are part of the blueprint, not optional commentary.
4. Nothing in this skill authorizes the architect to create evidence. When material is missing, the architect re-scopes, merges, defers, or requests retrieval.

## 9. Validation and stop conditions

The blueprint is complete only when all of the following hold:

- the fixed research brief is preserved;
- at least three candidate architectures were considered and the selection is recorded;
- the chapter roster carries 8–10 chapters with full responsibilities, sibling workplan, must_not_cover, and handoffs (or a documented defensible deviation);
- every claim has one owning chapter and an evidence obligation;
- evidence requirements and source-diversity constraints are stated per chapter;
- conflicts and gaps are recorded in the five-class task format;
- Qwen's intellectual ownership and the command-knowledge boundary are explicit.

Stop when any one of the following is true:

- `validate_review_blueprint_package` returns VALIDATION_PASSED and the completeness checks above all pass;
- all blocking structural gaps have been converted into retrieval tasks and the blueprint is validated with those tasks recorded;
- budget (iterations, tokens, or wall time) is reached and the partial blueprint honestly records what is missing.

## 10. Provenance

This skill is versioned command knowledge. Machine-readable provenance, including source project, commit, attribution metadata, and adoption modes, is in `provenance.json` in this directory. Downstream code must load command knowledge through the skill-guidance contract so it is never mistaken for scientific evidence.
