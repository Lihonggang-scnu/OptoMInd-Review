# Staged Article Completion

Status: staged schema/runner with live Qwen provider plumbing and per-stage
model-tier routing. Live stage providers are available through
`make_qwen_stage_provider` (single stage) and
`make_multi_reviewer_qwen_provider` (whole-manuscript reviewers). Qwen fills
only content cells; local code owns schema, IDs, fingerprints, status, and
provenance.

## Staged order

1. `handoff_metadata_repair`
2. `commander_structure`
3. `conclusion`
4. `introduction`
5. `abstract`
6. `whole_manuscript_review`
7. `bounded_patch_proposals`
8. `editorial_revision`
9. `visual_remount`
10. `assembly_preflight`

Conclusion is drafted before introduction; the introduction plan derives its
promises from the conclusion/body; the abstract is last and compresses the
accepted package. After the whole-manuscript review and bounded patch
proposals, `editorial_revision` materializes only safe bounded local edits
into a new assembled manuscript; it never overwrites upstream chapter
assets.

## Contracts

All staged contracts live in
`optomind_research/runtime/staged_article_completion.py` and are re-exported
from `optomind_research/runtime/article_completion_schemas.py`:

- `CommanderStructuralAuthority` — article-level order/responsibility,
  duplication, missing axes, structure gaps, and visual work orders, with
  `claim_evidence_invariant="preserved"` and explicit
  `approval_required_for`.
- `ConclusionWorkplan/Draft`, `IntroductionWorkplan/Draft`,
  `AbstractWorkplan/Draft`. `IntroductionWorkplan` carries an additive
  `retrieval_proposals` list used only when local background context is
  genuinely insufficient; the stage never launches retrieval.
- `ManuscriptReviewFinding/Report` — advisory unless severity is critical with
  full multi-reviewer consensus. Findings accept an optional stable
  `issue_key` (or `issue_type` + `target_ids`) so materially identical
  findings aggregate without exact prose equality; the local runtime owns
  finding IDs and status fields.
- `BoundedPatchProposal/PatchProposalSet` — semantic operations always set
  `approval_required=True`.
- `EditorialWorkItem`, `EditorialVerification`, `EditorialRevisionRecord`,
  and `EditorialRevisionAudit` — bounded editorial work items, the
  independent verifier verdict, per-item audit records, and the stage audit
  that keeps reviewer findings and records any critical full-consensus
  issues that could not be safely materialized.

## Runner

`staged_article_completion.run_staged_article_completion(...)` is
deterministic and offline.  It writes one artifact per stage
(`staged_<stage>.json`), fingerprints each stage input/payload, persists
`staged_article_completion_state.json`, and resumes stages as no-op when input
fingerprints are unchanged.  Live providers can be injected via
`make_qwen_stage_provider` (and `make_multi_reviewer_qwen_provider` for the
whole-manuscript stage); local code owns the envelope, IDs, fingerprints,
status, and provenance.

Optional `stage_inputs: Mapping[str, Mapping[str, Any]]` supplies per-stage
inputs; each stage receives `stage_inputs.get(stage, inputs)` as its selected
inputs, and those selected inputs are included in the stage input fingerprint.
When `stage_inputs` is omitted, every stage uses the global `inputs` (existing
behavior is unchanged).  The stage provider receives the selected stage
inputs plus `previous_artifacts` for earlier stages.

## Live provider plumbing (increment 2)

`QwenStagedProvider` and `make_qwen_stage_provider` send one stage-specific
prompt plus the JSON stage input through `llm.qwen_chat_client.call_qwen_chat`
with a JSON response format.  Fenced JSON is accepted; at most one compact
repair call is made on parse failure.  High-information fields are returned in
the payload; usage metadata is returned in the reserved `_usage` key and the
runner stores it in `StagedStageState.usage` separately from `payload`.  API
keys and raw provider headers are never persisted.

Stage prompt files: `prompts/Staged Conclusion Author.txt`,
`prompts/Staged Introduction Author.txt`, `prompts/Staged Abstract Author.txt`,
`prompts/Staged Whole Manuscript Reviewer.txt`.

`MultiReviewerReport`/`ReviewerRole` represent independent reviewers, and
`aggregate_multi_reviewer_report` groups by stable issue identity and
computes deterministic consensus. Canonical `issue_type` plus sorted
`target_ids` take precedence; free-text `issue_key` is only a fallback, and
the aggregated `issue_key` is generated locally with `issue_type`/`target_ids`
preserved. Blocking requires full multi-reviewer consensus (every configured
reviewer flags the same canonical issue) AND every one of them assigns
critical severity; a critical/split-severity group stays advisory/fail-open
(`advisory = not blocking`). Consensus counts distinct reviewer identities,
never raw finding counts: duplicate same-reviewer findings for one canonical
issue merge into a single vote (higher severity wins), and blank/duplicate
`reviewer_id` values get local stable slots so configured calls stay
independent. Model-provided `consensus`/`blocking`/`advisory` values, finding
IDs, and free-text issue keys are overridden locally; raw per-reviewer
`issue_type`/`target_ids` are retained for audit. Reviewers never receive
another reviewer's outputs, and only the five editorial dimensions
(continuity, clarity, reader_flow, logic, overlap) are accepted.
`make_multi_reviewer_qwen_provider` defaults to those five roles, invokes the
whole-manuscript prompt once per configured reviewer role/id, aggregates
their findings, and merges per-reviewer usage into `_usage`.

## Editorial revision (increment: bounded materialization)

`editorial_revision` runs after `bounded_patch_proposals` and before
`visual_remount`. It converts actionable patch proposals and review findings
into bounded work items (one per canonical issue + target block, deduplicated
locally). Each author call sees only the target block, its immediate
neighbors, the section responsibility/reader-state context, and the relevant
finding or commander patch; no single LLM is ever asked to output the full
article.

Supported auto editorial scope: transitions, connectives/coreference,
duplicate redirect/cross-reference wording, and terminology clarification
that does not add scientific facts. Claim/citation/evidence/scope changes
are never auto-applied. Paragraph is the default unit; sentence-level edits
are limited to grammar/connectives/coreference. A `logic_conflict` finding
is never auto-mapped to a wording edit (it may be substantive): it stays
recorded/unresolved unless the commander explicitly emits a safe editorial
operation. Reference normalization (`normalize_reference`) is deterministic
elsewhere and is never routed through an LLM rewrite. Section-level
transition findings resolve to exactly one boundary edit: the first body
block of the later/destination section, using the last body block of the
source as previous context; a single-section or ambiguous target produces at
most one conservative item or stays advisory. From commander patch
proposals, only `rewrite_transition` (a bounded wording-only transition
edit) is auto-materializable by `editorial_revision`; structural operations
such as `move_block` remain proposals and are never auto-applied.
Each aggregated finding materializes at most one work item, so reviewer
`target_ids` are context/evidence locations, never a blanket rewrite list:
duplication/cross-reference findings deterministically pick the latest
resolvable body target (first explanation stays; a later repetition may
become a cross-reference), terminology-clarification findings pick the
earliest (term explained at first use), and unresolvable findings stay
advisory/fail-open. Total work items are bounded by proposals + findings,
not target count. Only major/critical findings generate automatic work
items; advisory/minor findings stay in `audit.review_findings_kept` and
`audit.unselected` provenance. At most ONE work item is planned per target
block across all kinds: an explicit `rewrite_transition` proposal wins,
otherwise higher severity wins, then stable document/kind order; losing
findings/proposals are recorded in `audit.unselected` and are never merged
into one combined edit prompt.

Every proposed changed block is checked by a second independent compact
Qwen verifier (meaning/scope/citations/numbers/conditions preserved and the
requested editorial problem improved) and by a local deterministic check
that the replacement keeps the exact `[REF:...]` marker sequence and
multiplicity (reordering markers is rejected; pure transition text may not
add REF markers). No scientific keyword hardcoding is used. Author
failures, verifier rejections, and format violations keep the original
block, record an audit advisory, and complete fail-open. A critical
full-consensus finding that cannot be safely materialized is recorded in
`audit.blocking_unresolved` for later/manual handling; ordinary issues never
block final assembly. Only one bounded revision pass runs per stage
execution.

The stage output payload contains `manuscript` (structured assembled
manuscript including the staged conclusion, introduction, and abstract,
plus a `full_text` rendering with accepted revisions applied) and `audit`
(per-item records with source finding/patch, original/revised hashes,
author/verifier usage, and status). Sections are assembled in
commander-selected order with visible headings injected exactly once for
Abstract, Introduction, every section, and Conclusion. When a section's
EXPLANATION_BLOCKS do not reconstruct its final `full_text`, the full text
is authoritative: edits apply only to exact, uniquely locatable block prose
and the complete original section is retained otherwise, so enhanced chapter
prose outside the blocks is never dropped. Accepted changes are applied only
to this new artifact; upstream chapter assets and manifests are never
written.

After the `editorial_revision` stage the local runner also writes a
standalone `STAGED_COMPLETE_REVIEW_EN.md` plus
`STAGED_COMPLETE_REVIEW_EN.sha256.json` (sha256 and provenance, including
the editorial-revision fingerprint). Resume/no-op runs regenerate the same
artifacts without touching upstream files.

Per-item checkpointing: when the runner passes an execution context
(`work_dir`/`resume`), every completed work item is atomically persisted to
`staged_editorial_revision_checkpoint.json`. The checkpoint fingerprint
covers the editorial input fingerprint, the exact planned work items,
author/verifier prompt hashes, model tiers, and token budgets. On `--resume`
with a matching fingerprint, completed items are reused (records, revisions,
and usage) and Qwen is called only for unfinished items; a mismatched
fingerprint or a non-resume run starts a fresh checkpoint. A crash after
item N leaves N reusable records, and the final audit/usage aggregation is
identical whether items were computed or resumed. Checkpoints never contain
secrets. Editorial author calls default to about 2500 max tokens and the
verifier to about 1200 max tokens (one paragraph per call, one JSON repair
maximum); conclusion/introduction/abstract budgets are unchanged.

Prompt files carry stage-specific soft word targets (conclusion 500-900,
introduction 800-1300, abstract 220-300). These are advisory targets, not
hard gates, and the local runner never enforces word counts.

## CLI

`scripts/run_staged_article_completion.py` is the standalone entry point:

```powershell
py -3.11 scripts/run_staged_article_completion.py `
  --inputs-json inputs.json `
  --output-dir staged_out `
  [--stage-inputs-json stage_inputs.json] `
  [--metadata-json metadata.json] `
  [--handoff-json handoff.json] `
  [--commander-work-order-json work_order.json] `
  [--run-id run-1] `
  [--resume] `
  [--model-tier c_model] `
  [--stage-model-tier conclusion=c_model] `
  [--stage-model-tier whole_manuscript_review=c2_model] `
  [--editorial-verifier-tier c2_model] `
  [--live-stages conclusion,whole_manuscript_review] `
  [--reviewer-roles continuity,clarity,reader_flow,logic,overlap]
```

`--handoff-json` injects the unified full-manuscript handoff package into the
global inputs as `full_manuscript_handoff` (+ `full_manuscript_handoff_path`),
and `--commander-work-order-json` injects the commander work order as
`commander_work_order` (+ `commander_work_order_path`); explicit CLI files
override the same keys from `--inputs-json`.

Model routing is per stage. By default (no tier flags), conclusion,
introduction, abstract, and bounded_patch_proposals route to `c_model`
(Qwen 3.5 Plus) and whole_manuscript_review routes to `c2_model`
(Qwen 3.7 Flash). An explicit `--model-tier` preserves the old fallback:
it applies to every live stage, and repeatable `--stage-model-tier STAGE=TIER`
entries override it per stage. Unknown stages, unsupported tiers, malformed
`STAGE=TIER` entries, and duplicate stage entries fail with a clear error.
The CLI summary records the actual per-stage tiers in `stage_model_tiers`
(also persisted in each staged artifact's `usage.model_tier`).

`--editorial-verifier-tier` selects the independent verifier model for the
`editorial_revision` stage (default `c2_model`, Qwen 3.7 Flash); the author
side uses the stage's model tier (default `c_model`, Qwen 3.5 Plus).

The staged context builder now emits an explicit `editorial_revision` stage
input (sections with stable block context, current commander section order,
and reviewer/patch/front-back source declarations). Local background
candidates from explanatory ledgers are enriched with abstract, year,
venue, authors, DOI/paper identity, and permission/trust metadata with
deterministic dedupe and no arbitrary cap. Conclusion receives
`user_question`, `problem_understanding`, `global_review_thesis`,
`global_narrative_strategy`, and commander structure; introduction receives
those identities plus the enriched local abstracts; abstract keeps its
article identity. Historical review summaries remain advisory context and
never replace the current `full_text`.

## Stage context preparation

`scripts/build_staged_manuscript_context.py` deterministically prepares
stage-specific context from `UNIFIED_MANUSCRIPT_HANDOFF.json` plus the global
commander work order:

```powershell
py -3.11 scripts/build_staged_manuscript_context.py `
  --project-root . `
  --handoff-json outputs/full_manuscript_handoff_v1/UNIFIED_MANUSCRIPT_HANDOFF.json `
  --commander-work-order-json outputs/global_commander/global_commander_work_order.json `
  --output-dir outputs/staged_context_v1
```

Outputs `STAGED_GLOBAL_INPUTS.json` (normalized per-section text with stable
block IDs/hashes, theses, terminology, review summaries, citation inventory,
local background candidates ranked only by existing selection scores, and
commander structure) and `STAGED_STAGE_INPUTS.json` (stage-specific inputs
for conclusion, introduction, abstract, whole_manuscript_review, and
bounded_patch_proposals, with soft word targets).  All project-relative paths
are resolved from `project_root` and digests are validated against the
handoff before use; the fingerprint depends on content only, and no evidence
is promoted (core packets stay core, explanatory ledgers stay background).

Only stages named in `--live-stages` receive Qwen providers
(`make_qwen_stage_provider` for conclusion/introduction/abstract/
bounded_patch_proposals and `make_multi_reviewer_qwen_provider` for
whole_manuscript_review); all other stages use deterministic offline
providers.  Unknown or unsupported live stage names, invalid
`--stage-model-tier` entries, and missing/invalid JSON files fail with a
clear error and a nonzero exit code.  The CLI prints a compact JSON summary
with status, per-stage statuses, approval-required stages, actual per-stage
model tiers, and total recorded input/output tokens when available.

## Invariants

- No silent claim/evidence changes: `validate_claim_evidence_invariant_preserved`
  rejects payloads carrying claim/evidence rewrite fields.
- Advisory prose findings are fail-open; only critical severity with
  full multi-reviewer consensus where every reviewer assigns critical
  severity becomes blocking. Reviewer-provided consensus/blocking flags are
  ignored and recomputed locally, and `advisory` derives from blocking so
  nonblocking critical findings stay visible as advisory.
- Findings group by canonical `issue_type` + sorted `target_ids` before the
  free-text `issue_key` fallback and prose fallback; the local runtime
  generates the aggregated `issue_key`, preserves normalized identity/target
  IDs, and owns finding IDs.
- Editorial revisions are bounded, fail-open, and never overwrite upstream
  assets; the assembled manuscript lives only in the
  `staged_editorial_revision.json` artifact, and only local REF-marker
  sequence identity plus verifier approval can accept a change.
- Word targets in prompts are soft targets and are not hard gates.
- The existing `run_article_completion` and `run_global_manuscript_commander`
  contracts are unchanged.
