---
name: section-literature-coverage
description: Methodology for building an auditable literature coverage package for a single review chapter section.
---

# Section Literature Coverage Methodology

This skill describes the working method for researching and materializing literature for one review section. It does not contain domain knowledge or pre-loaded answers.

## Step 1 — Load and understand the section context

Call `load_section_context` first. Read:
- The chapter argument (what claim the section must support).
- The scope guardrails (topics explicitly out of scope).
- The required and optional coverage roles.

Do not search for anything until you understand what the section is arguing.

## Step 2 — Audit local coverage before going online

Call `inspect_section_local_coverage` to query the local knowledge base.
For each of the six roles (foundation, mechanism, method, frontier, controversy, application), record:
- How many papers and chunks already exist.
- Which roles are sufficient, partial, or missing entirely.

The hits returned by this step are recall candidates, not accepted writing
material. Inspect useful candidates with `inspect_local_candidate_batch`, then
record an explicit decision with `submit_local_source_audit`. Approve only when
the exact chunk is within the section scope and genuinely performs the proposed
literature role. A paper may be contextual without being suitable for pivotal
claims.

Use role semantics, not keyword labels:
- foundation = historical or conceptual basis;
- mechanism = causal physics or governing model;
- method = the section-specific procedure for design, measurement, fabrication,
  characterization, or analysis;
- frontier = an advance that changes the capability boundary;
- controversy = conflicting evidence, disputed definitions/measurements, or an
  unresolved scientific disagreement (a limitation alone is not controversy);
- application = deployment, integration, use-case evidence, or constraints.

**Local first. Audit before adoption. Online only for genuine gaps.**

## Step 3 — Plan proportionally

Call `submit_literature_role_plan` with a plan for all six roles.

**For roles already covered locally:** set priority = "not_needed" or "useful".
**For blocking gaps:** set priority = "required" with 2–3 targeted queries derived from the chapter argument — not keyword copies from the section title.

Keep tasks coarse-grained: one entry per role gap, not one entry per tool call.

## Step 4 — Query local KB for each gap

Before calling any external search, call `query_review_knowledge_base` with a query targeted at each blocking or important gap.
Use role-specific language, not generic keywords.

## Step 5 — Search external sources for unresolved gaps

For each gap that remains after local query, call `search_oa_candidates` with
2–3 focused queries. Use no more than three strategically different rounds for
one role. A later round is justified only when the previous round yielded no
usable source or left a consequential gap; change vocabulary, mechanism,
application analogy, or citation seed rather than repeating the query.
The tool persists and enforces this limit across restart/resume. If it returns
`role_search_round_limit_reached`, `consecutive_no_yield_stop`,
`duplicate_search_round`, or `candidate_audit_required`, do not work around the
guard. Audit what is already present or document the gap.

When a useful paper contains a pivotal cited claim, use
`trace_seed_references` to follow that source before another broad search.

## Step 6 — Inspect and audit candidates

Call `inspect_candidate_batch` to review full abstracts.
Then call `submit_candidate_audit` with your scope/role decisions:
- `direct`: paper is directly about the section's topic.
- `adjacent`: paper is from a related domain — useful for context and method transfer.
- Adjacent papers must not donate another application's standalone examples or
  quantitative results to the target section. Use them only for explicitly
  transferable mechanisms, methods, and bounded synthesis.
- `contextual`: background only — not for exact claims.
- `out_of_scope`: do not acquire.

Record `not_usable_for` for any paper that cannot support exact measurements or causal claims without evidence.

## Step 7 — Acquire approved candidates

Call `acquire_and_materialize_oa_papers` for approved candidates only. The
runtime accepts the useful approved candidates as one bounded batch, processes
them in direct-first order, and deterministically checks after every paper.
The batch stops immediately when the current package is sufficient.
Full OA text is preferred; abstract-only is acceptable if no OA full text is available.

## Step 8 — Refresh and reassess

The materialization tool and provider both refresh coverage after durable
evidence changes. Call `refresh_section_coverage` when the returned summary
needs inspection, not once per paper. If the paper cap is reached first, the
provider records an explicit open gap rather than silently treating the
shortfall as coverage.
Decide whether remaining gaps are still blocking or have become acceptable.
Role completeness does not equal review-quality breadth. Meet the
`minimum_unique_sources` and `minimum_direct_sources` returned by the context
tool, using useful local sources first. When those targets cannot be reached
after defensible retrieval, document a `coverage_breadth` gap and its stop
reason instead of silently presenting a one-paper section as comprehensive.

## Step 9 — Document gaps honestly

For each unresolved gap, call `submit_section_gap_report` with a JSON object:
```json
{
  "gaps": [
    {
      "role": "<role>",
      "severity": "blocking|important|minor",
      "description": "<what is missing and why it matters>",
      "queries_attempted": ["<query1>", "..."],
      "candidates_found": 0,
      "candidates_approved": 0,
      "candidates_materialized": 0,
      "stop_reason": "<why search stopped: no OA fulltext / no relevant candidates / budget>",
      "suggested_followup": "<what to try next>",
      "is_blocking": true
    }
  ],
  "overall_coverage_status": "coverage_sufficient|completed_with_open_gaps|blocking_gaps_remain"
}
```

**Do not fabricate sources or claim coverage you do not have.**

## Step 10 — Validate

Call `validate_section_coverage_package` when coverage is ready or all gaps are documented.
Only declare the task complete after receiving `VALIDATION_PASSED`.

## Stop conditions (any one is sufficient)

- All required roles are covered and the source-breadth targets are met.
- Remaining gaps are non-blocking (important or useful only) and documented.
- Two consecutive, strategically different rounds found no new usable candidates.
- Budget reached (queries or downloads).
- External backends returned errors or zero results for all queries.
